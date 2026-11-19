package com.pulse.target

import java.math.BigDecimal

data class CartItem(val name: String, val price: BigDecimal, var qty: Int)

/** Pure cart model so amount logic and injected faults are unit-testable. */
class Cart(val items: List<CartItem>) {
    fun inc(i: Int) { items[i].qty += 1 }
    fun dec(i: Int) { if (items[i].qty > 0) items[i].qty -= 1 }

    fun total(): BigDecimal = items.fold(BigDecimal.ZERO) { acc, it -> acc + it.price * it.qty.toBigDecimal() }

    fun displayedQty(i: Int, faults: Set<Fault>): Int =
        items[i].qty + if (Fault.CART_QTY_OFF_BY_ONE in faults) 1 else 0

    fun displayedTotal(faults: Set<Fault>): BigDecimal =
        total() + if (Fault.CART_TOTAL_WRONG in faults) BigDecimal("0.01") else BigDecimal.ZERO

    fun visibleIndexes(faults: Set<Fault>): List<Int> =
        items.indices.filter { !(Fault.CART_ITEM_MISSING in faults && it == 1) }

    companion object {
        fun default() = Cart(
            listOf(
                CartItem("拿铁", BigDecimal("32.00"), 1),
                CartItem("美式", BigDecimal("28.50"), 2),
                CartItem("可颂", BigDecimal("18.00"), 1),
            ),
        )

        fun money(v: BigDecimal): String = "¥" + v.setScale(2).toPlainString()
    }
}
