package com.pulse.target

import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle
import android.widget.TextView

class MainActivity : BaseActivity() {
    private var faultsView: TextView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(column {
            text(R.id.tv_home_title, "Pulse 靶场 v1", 22f)
            faultsView = text(R.id.tv_active_faults, "")
            button(R.id.btn_open_list, "原生列表") { open(ListActivity::class.java) }
            button(R.id.btn_open_form, "表单") { open(FormActivity::class.java) }
            button(R.id.btn_open_cart, "购物车") { open(CartActivity::class.java) }
            button(R.id.btn_open_webview, "WebView 页") { open(WebViewActivity::class.java) }
            button(R.id.btn_open_dialog, "弹窗") { open(DialogActivity::class.java) }
            button(R.id.btn_open_delayed, "延迟加载") { open(DelayedActivity::class.java) }
            button(R.id.btn_open_flutter, "Flutter 积分兑换") { open(FlutterPageActivity::class.java) }
        })
        if (isOn(Fault.POPUP_ON_LAUNCH)) showPromoPopup()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        Faults.applyIntent(this, intent)
    }

    override fun onResume() {
        super.onResume()
        val active = Faults.active(this)
        faultsView?.text = "已开启错误注入：" +
            if (active.isEmpty()) "无" else active.joinToString(", ") { it.key }
    }

    private fun showPromoPopup() {
        AlertDialog.Builder(this)
            .setTitle("限时活动")
            .setMessage("新人专享：第二杯半价")
            .setPositiveButton("去看看") { d, _ -> d.dismiss() }
            .setNegativeButton("关闭") { d, _ -> d.dismiss() }
            .show()
    }

    private fun open(cls: Class<*>) = startActivity(Intent(this, cls))
}
