package com.pulse.target

import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/** Base activity: applies fault extras on every launch and offers small view helpers. */
abstract class BaseActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Faults.applyIntent(this, intent)
        // targetSdk 35+ is edge-to-edge: keep content out of system bars and the IME.
        findViewById<View>(android.R.id.content).setOnApplyWindowInsetsListener { v, insets ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                val b = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
                v.setPadding(b.left, b.top, b.right, b.bottom)
            } else {
                @Suppress("DEPRECATION")
                v.setPadding(
                    insets.systemWindowInsetLeft, insets.systemWindowInsetTop,
                    insets.systemWindowInsetRight, insets.systemWindowInsetBottom,
                )
            }
            insets
        }
    }

    protected fun isOn(fault: Fault) = Faults.isOn(this, fault)

    protected fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    protected fun column(scroll: Boolean = true, build: LinearLayout.() -> Unit): View {
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(16), dp(16), dp(16))
            build()
        }
        return if (scroll) ScrollView(this).apply { addView(col) } else col
    }

    protected fun ViewGroup.text(id: Int, value: String, size: Float = 16f): TextView =
        TextView(context).also {
            it.id = id
            it.text = value
            it.textSize = size
            it.setPadding(0, dp(6), 0, dp(6))
            addView(it)
        }

    protected fun ViewGroup.button(id: Int, label: String, onClick: () -> Unit): Button =
        Button(context).also {
            it.id = id
            it.text = label
            it.isAllCaps = false
            it.setOnClickListener { onClick() }
            addView(it)
        }
}
