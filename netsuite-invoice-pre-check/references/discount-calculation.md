# Discount calculation model (Check 1)

Use **decimal arithmetic** (e.g., Python `decimal.Decimal`), never binary
floating point. Round only once, at the end, to `ROUNDING_SCALE` (2) decimal
places using `ROUNDING_MODE` (`HALF_UP`).

## 1. Build the expected discount set

Evidence sources, in order:

1. **Customer Account Notes** — normal standing discount source.
2. **SO Memo** — expected to restate Account Notes; may add a special discount.
3. **`Seasonal Discounts`** rows effective for this customer on the SO `DATE`.
4. **`Special Discounts`** rows matching this SO / PO / customer.
5. **Outlook email** — only after the evidence policy is confirmed and the
   connector is available. Until then, any discount requiring email
   verification → `NEEDS-REVIEW`.

### Deduplication and conflicts

- Deduplicate across all sources on normalized
  `discount value + reason + item scope`.
- A Memo repetition of an Account Notes discount counts **once**.
- The same percentage with a different reason or different item scope remains a
  **separate** discount.
- `NEEDS-REVIEW` (do not guess): similar but non-identical evidence,
  contradictory values, a Memo-only special discount with no verified evidence,
  an unresolved email requirement, or prose that cannot be parsed confidently.
  Never invent a discount from ambiguous text.

## 2. Eligibility: ordered quantity; basis: shipped quantity

- An item threshold such as "over 64" means **64 or more** (XXX convention).
- Judge eligibility on the **ordered** quantity on the SO, aggregated across
  duplicate SO lines for the same Item Code.
- Once eligible, calculate the discount only on quantities in Item Fulfillments
  whose status is `Shipped`, aggregated by SO Line ID and Item Code.
- Example: ordered 64, shipped 63, item discount 3% → eligibility passes;
  the 3% applies to the 63 shipped units' value.

## 3. Calculation per scope

For each applicable scope (whole-order, or a specific Item Code):

1. Sum fixed-dollar (`AMOUNT`) discounts applicable to that scope.
2. Subtract the fixed dollars from the applicable **shipped** subtotal.
3. Compound all percentage discounts on the remaining base:
   `combined rate = 1 − Π(1 − rate_i)`
4. `pre-cap discount = fixed discounts + remaining base × combined rate`
5. If an order-level cap exists, cap the **entire SO discount**:
   `expected discount = min(pre-cap total discount, applicable cap amounts)`
6. Round the final dollar amount to cents, decimal `HALF_UP`.

### 工具的实际行为与上面这个模型的差异（2026-08-18 核对）

上面第 3 节是锁定方案定义的**模型**。`invoice-precheck-tool` 目前的实现有
三处不同，人工走这条路时也按下面的来：

- **封顶不自动套用。** 带 Cap 的规则一律 `NEEDS-REVIEW`，由人核最终折扣。
  第 5 步那个 `min(...)` 目前只存在于模型和 `expected_discount()` 里，生产
  路径不走。
- **条件性折扣一律不自动计算**：订单类型类（DropShip / 中国单 / 整托 /
  自提 / 开户 / 促销）、以及「先 X% 再 Y%」式的连续折扣。认得出来、写进
  证据、交人工，绝不替人决定适不适用。
- **订单级百分比目前打在 SO 的整单小计上**，不是已发货小计；只有按料号
  的折扣用已发货金额。整单发完时两者一致，**部分发货的单会有差异** ——
  这一条与本文件第 2 节冲突，尚未定论（审计 M5，待 Jing 用一张实际的
  Partially Fulfilled 发票确认 NetSuite 是按哪个基数打折）。

### Worked examples

- **Compounding, no fixed:** 10% + 3% → `1 − 0.90 × 0.97 = 0.127` = 12.7%,
  **not** 13%.
- **Fixed then percentages:** $10,000 applicable base, $1,000 fixed, then 10%
  and 5%: base after fixed = $9,000; combined rate `1 − 0.90 × 0.95 = 0.145`;
  percentage part = $1,305; pre-cap total = $1,000 + $1,305 = **$2,305**. An
  order cap then limits that entire amount.

## 4. Compare against NetSuite

NetSuite representation on the SO:

- Items tab header `DISCOUNT ITEM` may be `Custom Discount` **or any preset
  discount item** (e.g. "8% Discount") — both are valid (XXX confirmed
  2026-08-17); only the amount matters.
- `RATE` may hold an equivalent order-level percentage **or** a fixed dollar
  value. Item-specific discounts may be entered as an equivalent whole-order
  percentage or as the calculated dollar amount — both are valid
  representations.

Verification:

- **Primary criterion:** the Summary `DISCOUNT ITEM` amount equals the expected
  discount **exactly to the cent** (`DISCOUNT_AMOUNT_TOLERANCE_USD = 0`).
- Also verify that
  `Subtotal − Discount Item + Shipping Cost + Misc. Cost = Total` using the
  displayed values.
- A mathematically equivalent Rate representation is acceptable; trailing
  zeros are irrelevant.
- `FAIL`: no discount item present when a discount is required; an unexplained
  discount when none is due; any cent difference.
