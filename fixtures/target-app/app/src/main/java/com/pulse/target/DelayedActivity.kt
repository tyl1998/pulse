package com.pulse.target

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.ProgressBar

class DelayedActivity : BaseActivity() {
    private val loadHandler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val delay = Faults.delayMs(this)
        setContentView(column {
            val hint = text(R.id.tv_delayed_hint, "加载中（${delay}ms）…")
            val progress = ProgressBar(context).apply { id = R.id.pb_loading }.also(::addView)
            val content = text(R.id.tv_delayed_content, "加载完成").apply { visibility = View.GONE }
            val action = button(R.id.btn_delayed_action, "继续") { content.text = "已继续" }
                .apply { visibility = View.GONE }
            if (!isOn(Fault.DELAY_NEVER_LOADS)) {
                loadHandler.postDelayed({
                    progress.visibility = View.GONE
                    hint.visibility = View.GONE
                    content.visibility = View.VISIBLE
                    action.visibility = View.VISIBLE
                }, delay.toLong())
            }
        })
    }

    override fun onDestroy() {
        loadHandler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }
}
