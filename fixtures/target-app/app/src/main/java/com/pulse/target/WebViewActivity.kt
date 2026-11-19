package com.pulse.target

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient

class WebViewActivity : BaseActivity() {
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Allows future DOM-level inspection over DevTools; fixture only.
        WebView.setWebContentsDebuggingEnabled(true)
        val web = WebView(this).apply {
            id = R.id.wv_content
            settings.javaScriptEnabled = true
            settings.allowFileAccess = false
            webViewClient = WebViewClient()
        }
        setContentView(web)
        val wrong = if (isOn(Fault.WEBVIEW_TEXT_WRONG)) "1" else "0"
        // Local asset only; no network content.
        web.loadUrl("file:///android_asset/web/index.html?wrong=$wrong")
    }
}
