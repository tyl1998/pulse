package com.pulse.target

import android.os.Bundle
import android.widget.LinearLayout
import android.widget.TextView

class CartActivity : BaseActivity() {
    private val cart = Cart.default()
    private val qtyViews = mutableMapOf<Int, TextView>()
    private lateinit var totalView: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val faults = Faults.active(this)
        setContentView(column {
            text(R.id.tv_cart_title, "购物车", 20f)
            for (i in cart.visibleIndexes(faults)) addView(row(i))
            totalView = text(R.id.tv_cart_total, "", 18f)
            val result = TextView(context)
            button(R.id.btn_checkout, "去结算") {
                result.text = "下单成功，实付 " + Cart.money(cart.displayedTotal(Faults.active(this@CartActivity)))
            }
            addView(result.apply { id = R.id.tv_checkout_result })
        })
        refresh()
    }

    private fun row(i: Int): LinearLayout {
        val item = cart.items[i]
        return LinearLayout(this).apply {
            id = ROW_IDS[i]
            orientation = LinearLayout.HORIZONTAL
            text(NAME_IDS[i], item.name).layoutParams = LinearLayout.LayoutParams(0, -2, 2f)
            text(PRICE_IDS[i], Cart.money(item.price)).layoutParams = LinearLayout.LayoutParams(0, -2, 2f)
            button(MINUS_IDS[i], "-") { cart.dec(i); refresh() }.contentDescription = "减少${item.name}"
            qtyViews[i] = text(QTY_IDS[i], "")
            button(PLUS_IDS[i], "+") { cart.inc(i); refresh() }.contentDescription = "增加${item.name}"
        }
    }

    private fun refresh() {
        val faults = Faults.active(this)
        qtyViews.forEach { (i, v) -> v.text = cart.displayedQty(i, faults).toString() }
        totalView.text = "合计：" + Cart.money(cart.displayedTotal(faults))
    }

    companion object {
        private val ROW_IDS = intArrayOf(R.id.cart_row_1, R.id.cart_row_2, R.id.cart_row_3)
        private val NAME_IDS = intArrayOf(R.id.tv_cart_name_1, R.id.tv_cart_name_2, R.id.tv_cart_name_3)
        private val PRICE_IDS = intArrayOf(R.id.tv_cart_price_1, R.id.tv_cart_price_2, R.id.tv_cart_price_3)
        private val QTY_IDS = intArrayOf(R.id.tv_cart_qty_1, R.id.tv_cart_qty_2, R.id.tv_cart_qty_3)
        private val MINUS_IDS = intArrayOf(R.id.btn_cart_minus_1, R.id.btn_cart_minus_2, R.id.btn_cart_minus_3)
        private val PLUS_IDS = intArrayOf(R.id.btn_cart_plus_1, R.id.btn_cart_plus_2, R.id.btn_cart_plus_3)
    }
}
