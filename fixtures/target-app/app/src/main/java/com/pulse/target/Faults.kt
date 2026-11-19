package com.pulse.target

import android.content.Context
import android.content.Intent
import android.util.Log

/** Known fault switches. Names are the stable contract used by golden cases. */
enum class Fault(val key: String, val description: String) {
    CART_QTY_OFF_BY_ONE("cart_qty_off_by_one", "购物车显示数量比实际多 1"),
    CART_TOTAL_WRONG("cart_total_wrong", "购物车合计多 0.01"),
    CART_ITEM_MISSING("cart_item_missing", "购物车第 2 个商品不显示（合计仍包含）"),
    LIST_ITEM_MISSING("list_item_missing", "列表缺少第 30 项"),
    FORM_SUBMIT_MISSING("form_submit_missing", "表单提交按钮消失"),
    FORM_RESULT_WRONG("form_result_wrong", "表单结果中的姓名错误"),
    POPUP_ON_LAUNCH("popup_on_launch", "首页启动时弹出活动弹窗"),
    WEBVIEW_TEXT_WRONG("webview_text_wrong", "WebView 结果文本错误"),
    DELAY_NEVER_LOADS("delay_never_loads", "延迟页永远不加载完成"),
    FLUTTER_AMOUNT_WRONG("flutter_amount_wrong", "Flutter 页可用积分多 1"),
    FLUTTER_BUTTON_MISSING("flutter_button_missing", "Flutter 页兑换按钮消失");

    companion object {
        fun fromKey(key: String): Fault? = entries.firstOrNull { it.key == key }
    }
}

/** Parsed fault request; pure so it can be unit-tested without Android. */
data class FaultRequest(
    val clear: Boolean,
    val faults: Set<Fault>?,
    val unknown: List<String>,
    val delayMs: Int?,
) {
    companion object {
        fun parse(clear: Boolean, faultsCsv: String?, delayMs: Int?): FaultRequest {
            val names = faultsCsv?.split(',')?.map { it.trim() }?.filter { it.isNotEmpty() }
            val known = names?.mapNotNull { Fault.fromKey(it) }?.toSet()
            val unknown = names?.filter { Fault.fromKey(it) == null }.orEmpty()
            return FaultRequest(clear, known, unknown, delayMs?.takeIf { it >= 0 })
        }
    }
}

/**
 * Persistent fault state (SharedPreferences), switchable via intent extras:
 *  - `--ez clear_faults true`  clear all faults and delay
 *  - `--es faults "a,b"`       replace the active fault set
 *  - `--ei delay_ms 3000`      delay for the delayed-load page
 */
object Faults {
    const val EXTRA_FAULTS = "faults"
    const val EXTRA_CLEAR = "clear_faults"
    const val EXTRA_DELAY_MS = "delay_ms"
    const val DEFAULT_DELAY_MS = 2000
    private const val PREFS = "pulse_target_faults"
    private const val TAG = "PulseTarget"

    fun applyIntent(context: Context, intent: Intent?): FaultRequest? {
        if (intent == null) return null
        val hasAny = intent.hasExtra(EXTRA_FAULTS) || intent.hasExtra(EXTRA_CLEAR) ||
            intent.hasExtra(EXTRA_DELAY_MS)
        if (!hasAny) return null
        val req = FaultRequest.parse(
            clear = intent.getBooleanExtra(EXTRA_CLEAR, false),
            faultsCsv = intent.getStringExtra(EXTRA_FAULTS),
            delayMs = if (intent.hasExtra(EXTRA_DELAY_MS)) intent.getIntExtra(EXTRA_DELAY_MS, -1) else null,
        )
        apply(context, req)
        return req
    }

    fun apply(context: Context, req: FaultRequest) {
        val editor = prefs(context).edit()
        if (req.clear) editor.clear()
        req.faults?.let { set -> editor.putStringSet(EXTRA_FAULTS, set.map { it.key }.toSet()) }
        req.delayMs?.let { editor.putInt(EXTRA_DELAY_MS, it) }
        editor.commit()
        if (req.unknown.isNotEmpty()) Log.w(TAG, "unknown faults ignored: ${req.unknown}")
        Log.i(TAG, "faults=${active(context).map { it.key }} delay_ms=${delayMs(context)}")
    }

    fun active(context: Context): Set<Fault> =
        prefs(context).getStringSet(EXTRA_FAULTS, emptySet()).orEmpty()
            .mapNotNull { Fault.fromKey(it) }.toSortedSet(compareBy { it.ordinal })

    fun isOn(context: Context, fault: Fault): Boolean = fault in active(context)

    fun delayMs(context: Context): Int = prefs(context).getInt(EXTRA_DELAY_MS, DEFAULT_DELAY_MS)

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}
