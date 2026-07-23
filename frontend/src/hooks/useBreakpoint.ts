/**
 * RWD breakpoint hook —— 用 useWindowDimensions 對齊 Tailwind defaults
 *
 * Tailwind:
 *   sm  >= 640
 *   md  >= 768
 *   lg  >= 1024
 *   xl  >= 1280
 *
 * 用法:
 *   const bp = useBreakpoint();
 *   if (bp.isMd) { ... }   // >= 768
 *   bp.value === 'lg'      // 當前命中
 */
import { useWindowDimensions } from 'react-native';

export type Breakpoint = 'xs' | 'sm' | 'md' | 'lg' | 'xl';

export type BreakpointInfo = {
  width: number;
  height: number;
  value: Breakpoint;
  isSm: boolean;   // >= 640
  isMd: boolean;   // >= 768
  isLg: boolean;   // >= 1024
  isXl: boolean;   // >= 1280
};

export function useBreakpoint(): BreakpointInfo {
  const { width, height } = useWindowDimensions();
  const isSm = width >= 640;
  const isMd = width >= 768;
  const isLg = width >= 1024;
  const isXl = width >= 1280;
  let value: Breakpoint = 'xs';
  if (isXl) value = 'xl';
  else if (isLg) value = 'lg';
  else if (isMd) value = 'md';
  else if (isSm) value = 'sm';
  return { width, height, value, isSm, isMd, isLg, isXl };
}
