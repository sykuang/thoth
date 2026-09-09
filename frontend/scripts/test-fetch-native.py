#!/usr/bin/env python3
"""Offline macOS Swift regression for Expo's installed iOS body consumers.

Compiles NativeResponse unchanged and slices the actual AsyncFunction bodies.
Only module/JSI bridge types are stubbed: this is not a phone or JSI test.
Run with pnpm test:fetch-native (requires Python 3 and macOS Swift).
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


CORE = r"""
@_exported import Foundation
@_exported import CFNetwork
open class SharedObject {
  public init() {}
  public func emit<T>(event: String, payload: T) {}
  public func emit(event: String) {}
}
"""

SUPPORT = r"""
import Foundation
protocol ExpoURLSessionTaskDelegate {}
final class ExpoURLSessionTask {}
enum NativeRequestRedirect { case follow, manual, error }
struct NativeResponseInit {
  let headers: [[String]]
  let status: Int
  let statusText: String
  let url: String
}
final class FetchRequestCanceledException: Error {}
struct FetchRedirectException: Error {}
struct FetchUnknownException: Error {}
struct QuietLogger { func error(_ message: String) { fatalError(message) } }
let log = QuietLogger()
struct NativeArrayBuffer {
  let data: Data
  static func wrap(dataWithoutCopy data: Data) -> NativeArrayBuffer {
    NativeArrayBuffer(data: data)
  }
}
// All calls and observations use the response's serial queue.
final class Promise: @unchecked Sendable {
  var resolutions: [Any] = []
  var rejections: [Error] = []
  var count: Int { resolutions.count + rejections.count }
  func resolve(_ value: Any) { resolutions.append(value) }
  func reject(_ error: Error) { rejections.append(error) }
}
"""

MAIN = r"""
import Foundation
let url = URL(string: "https://synthetic.invalid/body")!
let task = URLSession.shared.dataTask(with: url) // Never resumed; no network.
let partial = Data("{\"status\":".utf8)
let remainder = Data("\"ok ✓\"}".utf8)
let complete = partial + remainder
let consumers: [(String, (NativeResponse, Promise) -> Void)] = [
  ("text", consume_text), ("arrayBuffer", consume_arrayBuffer)
]
var failures = 0
var cases = 0
for (name, consume) in consumers {
  for mode in ["success", "network-drop", "abort"] {
    for late in [false, true] {
      cases += 1
      let label = "\(name) / \(mode) / \(late ? "late" : "waiting")"
      let queue = DispatchQueue(label: label)
      let response = NativeResponse(dispatchQueue: queue)
      let wrapper = ExpoURLSessionTask()
      let promise = Promise()
      let original = NSError(domain: NSURLErrorDomain, code: NSURLErrorNetworkConnectionLost,
                             userInfo: [NSLocalizedDescriptionKey: "synthetic mid-body drop"])
      func check(_ condition: Bool, _ message: String) {
        if !condition {
          failures += 1
          print("FAIL \(label): \(message)")
        }
      }
      queue.sync {
        response.urlSessionDidStart(wrapper)
        response.urlSession(wrapper, didReceive: HTTPURLResponse(
          url: url, statusCode: 200, httpVersion: "HTTP/1.1",
          headerFields: ["Content-Type": "application/json"])!)
        response.urlSession(wrapper, didReceive: partial)
        precondition(response.responseInit?.status == 200)
        precondition(response.state == .responseReceived)
      }
      queue.sync {} // Drain queued state notifications before attaching a waiter.
      if !late {
        queue.sync { consume(response, promise) }
        queue.sync {} // waitFor registers its listener asynchronously.
        queue.sync { check(promise.count == 0, "settled before body completed") }
      }
      queue.sync {
        switch mode {
        case "network-drop":
          response.urlSession(wrapper, task: task, didCompleteWithError: original)
        case "abort":
          response.emitRequestCanceled()
        default:
          response.urlSession(wrapper, didReceive: remainder)
          response.urlSession(wrapper, task: task, didCompleteWithError: nil)
        }
      }
      queue.sync {} // Drain the terminal-state notification, including its callbacks.
      if late {
        queue.sync { consume(response, promise) }
        queue.sync {}
      }
      queue.sync {
        if mode == "success" {
          check(response.state == .bodyCompleted, "wrong terminal state")
          check(promise.count == 1 && promise.resolutions.count == 1,
                "expected one resolve, got \(promise.count) settlements")
          if name == "text" {
            check(promise.resolutions.first as? String == String(decoding: complete, as: UTF8.self),
                  "resolved text differs from complete body")
          } else {
            check((promise.resolutions.first as? NativeArrayBuffer)?.data == complete,
                  "resolved bytes differ from complete body")
          }
        } else {
          check(response.state == .errorReceived, "wrong terminal state")
          check(promise.count == 1 && promise.rejections.count == 1,
                "expected one reject, got \(promise.count) settlements (body waiter stranded)")
          check(response.error != nil, "terminal failure lost response.error")
          if let error = promise.rejections.first, let stored = response.error {
            check((error as AnyObject) === (stored as AnyObject), "replaced response.error")
            if mode == "network-drop" {
              check((error as NSError) === original, "lost original network error")
            } else {
              check(error is FetchRequestCanceledException, "lost cancellation error")
            }
          }
          check(promise.resolutions.isEmpty, "resolved partial body after failure")
        }
        let outcome = promise.count == 0 ? "pending" :
          (promise.rejections.isEmpty ? "resolved" : "rejected")
        print("\(label): \(outcome)")
      }
    }
  }
}
precondition(task.state == .suspended, "test must not start networking")
print("\(failures == 0 ? "PASS" : "FAIL"): \(cases) cases, \(failures) failed checks; installed Swift, bridge stubs only")
exit(failures == 0 ? 0 : 1)
"""


def consumer_body(source, name):
    """Fail closed on duplicate names or a changed closure/queue boundary."""
    anchor = f'AsyncFunction("{name}")'
    pattern = (
        r'^      ' + re.escape(anchor)
        + r' \{ \(response: NativeResponse, promise: Promise\) in\n'
        + r'((?:^ {8}[^\n]*\n|^\n)+)'
        + r'^      \}\.runOnQueue\(fetchRequestQueue\)$'
    )
    matches = re.findall(pattern, source, re.MULTILINE)
    if source.count(anchor) != 1 or len(matches) != 1:
        raise RuntimeError(f"Cannot uniquely extract {name} AsyncFunction body")
    return matches[0]


def main():
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        print("UNSUPPORTED: test:fetch-native requires macOS and swiftc", file=sys.stderr)
        return 2
    expo = Path(__file__).resolve().parents[1] / "node_modules/expo"
    fetch = expo / "ios/Fetch"
    source = (fetch / "ExpoFetchModule.swift").read_text()
    consumers = "import ExpoModulesCore\n" + "\n".join(
        f"func consume_{name}(_ response: NativeResponse, _ promise: Promise) {{\n"
        + consumer_body(source, name) + "}\n"
        for name in ("text", "arrayBuffer")
    )
    print(f"Installed Expo {json.loads((expo / 'package.json').read_text())['version']}", flush=True)
    with tempfile.TemporaryDirectory(prefix="expo-fetch-native-") as directory:
        temp = Path(directory)
        for name, content in {
            "ExpoModulesCore.swift": CORE,
            "Support.swift": SUPPORT,
            "Consumers.swift": consumers,
            "main.swift": MAIN,
        }.items():
            (temp / name).write_text(content)
        for name in ("NativeResponse.swift", "ResponseState.swift"):
            shutil.copyfile(fetch / name, temp / name)
        # ResponseSink relies on the iOS target's implicit Foundation import.
        (temp / "ResponseSink.swift").write_text(
            "import Foundation\n" + (fetch / "ResponseSink.swift").read_text()
        )
        subprocess.run([
            "swiftc", "-emit-module", "-emit-library", "-module-name", "ExpoModulesCore",
            "ExpoModulesCore.swift", "-o", "libExpoModulesCore.dylib",
        ], cwd=temp, check=True, timeout=120)
        subprocess.run([
            "swiftc", "-I", ".", "-L", ".", "-lExpoModulesCore",
            "-Xlinker", "-rpath", "-Xlinker", str(temp),
            "NativeResponse.swift", "ResponseState.swift", "ResponseSink.swift",
            "Support.swift", "Consumers.swift", "main.swift", "-o", "probe",
        ], cwd=temp, check=True, timeout=120)
        return subprocess.run([str(temp / "probe")], cwd=temp, timeout=30).returncode


if __name__ == "__main__":
    sys.exit(main())
