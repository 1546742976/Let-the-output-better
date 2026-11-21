/**
 * CPython 数值口径的移植件：`round(x, 3)`、`f"{x:.2f}"`、`math.fsum`。
 *
 * 本项目把"同一输入必得同一得分"当作对外契约，而 JS 的 `toFixed`/`Math.round`
 * 在 .5 处按"取较大"处理，与 CPython 的 half-to-even 不一致；先乘后舍还会引入
 * 二进制误差（0.235 * 100 = 23.4999…）。这里按 IEEE-754 的精确有理值舍入。
 */

const POW10: bigint[] = Array.from({ length: 21 }, (_unused, index) => 10n ** BigInt(index));

function decompose(absValue: number): { mantissa: bigint; exponent: number } {
  const buffer = new DataView(new ArrayBuffer(8));
  buffer.setFloat64(0, absValue);
  const bits = buffer.getBigUint64(0);
  const biasedExponent = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & ((1n << 52n) - 1n);
  if (biasedExponent === 0) return { mantissa: fraction, exponent: -1074 };
  return { mantissa: fraction | (1n << 52n), exponent: biasedExponent - 1075 };
}

/** 把 `value` 放大 `digits` 位小数并按 half-to-even 取整，返回带符号的整数值。 */
export function scaledRound(value: number, digits: number): { negative: boolean; units: bigint } {
  const negative = value < 0;
  const abs = Math.abs(value);
  if (!Number.isFinite(abs)) return { negative, units: 0n };
  const { mantissa, exponent } = decompose(abs);
  const scale = POW10[digits];
  let numerator: bigint;
  let denominator: bigint;
  if (exponent >= 0) {
    numerator = mantissa << BigInt(exponent);
    denominator = 1n;
  } else {
    numerator = mantissa;
    denominator = 1n << BigInt(-exponent);
  }
  numerator *= scale;
  const floor = numerator / denominator;
  const remainder = numerator % denominator;
  const twice = remainder * 2n;
  let units = floor;
  if (twice > denominator || (twice === denominator && units % 2n !== 0n)) units += 1n;
  return { negative, units };
}

/** `round(value, digits)`：返回舍入后的数值。 */
export function roundTo(value: number, digits: number): number {
  const { negative, units } = scaledRound(value, digits);
  const magnitude = Number(units) / Number(POW10[digits]);
  return negative ? -magnitude : magnitude;
}

/** `f"{value:.digitsf}"`：负零也保留符号，与 CPython 的格式化一致。 */
export function fixed(value: number, digits: number): string {
  const { negative, units } = scaledRound(value, digits);
  const asString = units.toString().padStart(digits + 1, "0");
  const splitAt = asString.length - digits;
  const body = digits > 0 ? `${asString.slice(0, splitAt)}.${asString.slice(splitAt)}` : asString;
  return negative ? `-${body}` : body;
}

/** `f"{value:.digits%}"`：先按二进制乘法放大 100 倍，再格式化。 */
export function percent(value: number, digits = 0): string {
  return `${fixed(value * 100, digits)}%`;
}

/** `math.fsum`：补偿求和，避免逐项相加在 3.11/3.12 之间漂 1 ULP。 */
export function fsum(values: number[]): number {
  let sum = 0;
  let compensation = 0;
  for (const value of values) {
    const updated = sum + value;
    if (Math.abs(sum) >= Math.abs(value)) compensation += (sum - updated) + value;
    else compensation += (value - updated) + sum;
    sum = updated;
  }
  return sum + compensation;
}
