/**
 * BankBadge — 圓型銀行縮寫徽章, 用銀行真實 brand color 著色.
 *
 * 中文兩字縮寫 + brand color 背景 = 一眼識別 + 不靠 PNG asset.
 *
 * Size 4 種 (跟 NativeWind text-X 對齊):
 *   - xs (28px)  → transactions row inline 用
 *   - sm (40px)  → account card / dashboard 小卡
 *   - md (56px)  → account card 主圖示
 *   - lg (80px)  → 詳細頁 hero
 */
import { Text, View } from 'react-native';

import { bankMeta } from '@/lib/banks';

const SIZE_PX: Record<Size, number> = { xs: 28, sm: 40, md: 56, lg: 80 };
const TEXT_CLASS: Record<Size, string> = {
  xs: 'text-micro',
  sm: 'text-small',
  md: 'text-h3',
  lg: 'text-h2',
};

type Size = 'xs' | 'sm' | 'md' | 'lg';

export function BankBadge({
  bank,
  size = 'sm',
  stale = false,
  rectangular = false,
}: {
  bank: string;
  size?: Size;
  /** stale=true → badge 半透明, 提醒此帳戶資料舊 */
  stale?: boolean;
  /** 交易明細使用橫向標籤；其他 surface 維持圓形。 */
  rectangular?: boolean;
}) {
  const meta = bankMeta(bank);
  const px = SIZE_PX[size];
  return (
    <View
      style={{
        width: rectangular ? 48 : px,
        height: px,
        borderRadius: rectangular ? 8 : px / 2,
        backgroundColor: meta.color,
        opacity: stale ? 0.5 : 1,
      }}
      className="items-center justify-center"
      testID={`bank-badge-${bank}`}
    >
      <Text
        className={`${TEXT_CLASS[size]} font-bold`}
        style={{ color: meta.fg }}
      >
        {meta.short}
      </Text>
    </View>
  );
}
