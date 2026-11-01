"""Read hash-checked public source at the immutable pre-diagnostic commit.

No application code is executed; missing Git history is a test failure.
"""
from functools import lru_cache
import hashlib
from pathlib import Path
import subprocess

BASELINE = "16f2d4b130f44973d346b420f3d486ac839f71ac"
ROOT = Path(__file__).resolve().parents[1]
SHA256 = {'backend/banks/cathay.py': '355ec1c72da1c887e25a9ceb2c56c4a046d0ac66410d682883661efd1530771b',
 'backend/banks/ctbc.py': '4c1ad7b341df1eb96dbefff4bd43130f83cbf6255966de64315bf91dfa28cbc6',
 'backend/banks/dbs.py': '2b97319e2d43321ef72ee8a061c0d357c573d1372ed1e33b8df7e028899ba89b',
 'backend/banks/esun.py': '3caebb010552aade95f67a3a72aaaf7b6b0b52302c3d7725e3b06a8a0f9f2b28',
 'backend/banks/esun_spa/collection.py': '25b1c48d5cf3cc80e7afe26fd70144f631af030af386dedc16927e092c3c9b11',
 'backend/banks/esun_spa/products.py': 'fb57c9b3eae9dd937344920abe893be90a94aa67b0ed695132b8f0a35043a6ab',
 'backend/banks/esun_spa/query.py': '0f51056355d41343ca63978dcab055d2637e184bbcfbcc5c0be75f0d737ea347',
 'backend/banks/fubon.py': 'b01cb63ca7ddc6e28935a0a96f883503e4192f1ac9576e8706303330cac20f93',
 'backend/banks/hsbc.py': 'a6fb68673ecd491ba9e2e7b25ce3b532c6c12d556ddcfe2dd53cb4bdebb258b2',
 'backend/banks/linebank.py': '7050e0e553c597fc970b92d12568b5361127f6a15607df5b5b70892a9036fde6',
 'backend/banks/rakuten.py': '223afb8cec6042ec6f9a5df038e93a7d712d45e193c82f2eca8bc6cb67acb905',
 'backend/banks/scb.py': 'dee3bd9542c2ac4de51e80c24fd713b6be2ad49e2f79de4e9bb576f0fe8bfd99',
 'backend/banks/scsb.py': '9953ef7f7a6c14f0e734ffd4fa243e2da001c1efc1a0de00f081b6d4ea0a7b0e',
 'backend/banks/sinopac.py': '67821c9e451cd872d8c79203d2ec34082a32e8514f8276042e0fecb1cb668b61',
 'backend/banks/taishin.py': '4b3df4ca6359596c4bd8981c24608849bf2debb15b26c915d4c687edd4cef23a',
 'backend/banks/ubot.py': '015632a128acfacadcbc2e87471bb8964b71868d00f7303aa4b1084ccbf48ae7',
 'backend/core/base.py': '44b363c4376d737b5db19f3a632e2ec0831cad6cc5cedd3f1c1e5070750c872d',
 'backend/core/login_checkpoints.py': 'd9561eb40af209562fdbdbaa2bf46027f22243e2ec3c0e822f0d8dc7c553503c',
 'backend/core/money.py': 'fddf802fe178d1860424c9db35604eec2be71642521e891af80cd6afc3ef7e3c',
 'backend/core/persist/_common.py': 'd3d3753d337588affaac2a37bf01cad6cd1745dbd60277540b3578827c5fb211',
 'backend/core/persist/esun.py': 'e91a9d9f99471deee0acf8825e76c1b62db8a256b49989d736790d8e20dbf198',
 'backend/core/persist/sinopac.py': 'fc641abf12caed3e984f2f51871b6485aba105b7a71f9588677fc56601c284d2'}


@lru_cache(None)
def original_source(path: str) -> str:
    expected = SHA256[path]
    try:
        raw = subprocess.check_output(
            ["git", "show", f"{BASELINE}:{path}"], cwd=ROOT, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        raise RuntimeError("Pinned diagnostic baseline is unavailable") from None
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Pinned diagnostic baseline hash mismatch")
    return raw.decode("utf-8")


AUTH_SHA256 = {'base.py': 'd90b6a4866520cacc3d75d7b1ecb3f1b163f5defc943c6503337647a7a1456f7',
 'cathay.py': 'd7680c0969ed0a8250c5671399bd79882a229649ccbe97d87703ecd432d019e7',
 'ctbc.py': '12e24b90624dbaa464c5a32cd6c7771a297a7c15d310db8f35ae539d7f603d36',
 'dbs.py': 'd8e99b1075aa0d7c1ecb5b685ecea5e14c28609f9fa414978a9a28d3d1eea751',
 'esun.py': 'c8a491c0da64b37dd6ba3ed1285b19c7ccddb4e41b701f6b16310e03624f403a',
 'fubon.py': '75fb5b03c24478ac608d8b70385661dc9450ba0e4ef696adfd1fb395a7856194',
 'hsbc.py': '52eeb6a08968c6e14d54de00d1e9b5785752cc4cf7cd6d7708b6fdc816fb6db8',
 'linebank.py': '3bddb5d0091dd67b6bd3c025b27e34a51803b493fa4daca5cc913f6e4c92b2f3',
 'login_checkpoints.py': '24335db171030cf312dd1d7b726ae9ff99bb577c6e833470bfa0b79958a4bf91',
 'rakuten.py': 'dd3f7d573b369a4e535392800fcb0c1b32219ad8838cd0addceda99e98da8ba2',
 'scb.py': '27c533129fd6ae35ede140bcb6fa5a6e3c0732c04bcebb52198e7fa107e908e4',
 'scsb.py': '8b015804e62839e9eb63fad6a75086a630c7c42b3c0bd6f1496b93c99f0f2f67',
 'sinopac.py': '8663b540c351c1094c9b1ff76099566f86715bba6d13f3be86aa9c52c3c26f1b',
 'taishin.py': 'eb5250f16ef9b4263b54d7d61a3df9cc87f0c1a8113029103d18ba81b7e684c3',
 'ubot.py': '6ef089489b45f3e0db6f345c3bccda6ee68e6a5d080c039de60e9b97cb4e9f8d'}


@lru_cache(None)
def auth_original_source(filename: str) -> str:
    expected = AUTH_SHA256[filename]
    try:
        raw = (ROOT / "tests/fixtures/auth_bridge_originals" / filename).read_bytes()
    except OSError:
        raise RuntimeError("Pinned auth baseline is unavailable") from None
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Pinned auth baseline hash mismatch")
    return raw.decode("utf-8")
