package com.pulse.target

import android.os.Bundle
import io.flutter.embedding.android.FlutterActivity

/**
 * Exported entry for the Flutter page so adb can start it with fault extras
 * (`am start -n com.pulse.target/.FlutterPageActivity --es faults flutter_amount_wrong`).
 * Active faults are forwarded to Dart as entrypoint args.
 */
class FlutterPageActivity : BaseActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val args = listOf(Faults.active(this).joinToString(",") { it.key })
        startActivity(FlutterActivity.withNewEngine().dartEntrypointArgs(args).build(this))
        finish()
    }
}
