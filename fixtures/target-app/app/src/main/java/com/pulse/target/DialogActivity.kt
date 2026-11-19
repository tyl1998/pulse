package com.pulse.target

import android.app.AlertDialog
import android.os.Bundle
import android.widget.Toast

class DialogActivity : BaseActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(column {
            val result = text(R.id.tv_dialog_result, "未操作")
            button(R.id.btn_show_alert, "删除地址") {
                AlertDialog.Builder(context)
                    .setTitle("确认删除？")
                    .setMessage("删除后不可恢复")
                    .setPositiveButton("确定") { _, _ -> result.text = "已删除" }
                    .setNegativeButton("取消") { _, _ -> result.text = "已取消" }
                    .setCancelable(false)
                    .show()
            }
            button(R.id.btn_show_toast, "显示 Toast") {
                Toast.makeText(context, "操作成功", Toast.LENGTH_SHORT).show()
            }
        })
    }
}
