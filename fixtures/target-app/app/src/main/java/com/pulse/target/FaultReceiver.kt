package com.pulse.target

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class FaultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val req = Faults.applyIntent(context, intent)
        resultCode = if (req != null && req.unknown.isEmpty()) 0 else 1
        resultData = "faults=" + Faults.active(context).joinToString(",") { it.key } +
            ";delay_ms=" + Faults.delayMs(context) +
            if (req?.unknown?.isNotEmpty() == true) ";unknown=" + req.unknown.joinToString(",") else ""
    }
}
