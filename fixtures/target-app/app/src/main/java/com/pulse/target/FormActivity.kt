package com.pulse.target

import android.os.Bundle
import android.text.InputType
import android.view.View
import android.widget.CheckBox
import android.widget.EditText
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.Switch

class FormActivity : BaseActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        lateinit var name: EditText
        lateinit var agree: CheckBox
        lateinit var notify: Switch
        lateinit var large: RadioButton
        setContentView(column {
            text(R.id.tv_form_title, "用户信息", 20f)
            name = EditText(context).apply {
                id = R.id.et_name; hint = "姓名"; inputType = InputType.TYPE_CLASS_TEXT
            }.also(::addView)
            EditText(context).apply {
                id = R.id.et_password; hint = "密码"
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            }.also(::addView)
            val group = RadioGroup(context).apply { id = R.id.rg_size; orientation = RadioGroup.HORIZONTAL }
            RadioButton(context).apply { id = R.id.rb_small; text = "小杯"; isChecked = true }.also(group::addView)
            large = RadioButton(context).apply { id = R.id.rb_large; text = "大杯" }.also(group::addView)
            addView(group)
            notify = Switch(context).apply { id = R.id.sw_notify; text = "接收通知" }.also(::addView)
            agree = CheckBox(context).apply { id = R.id.cb_agree; text = "同意协议" }.also(::addView)
            val submit = button(R.id.btn_submit, "提交") {}
            val result = text(R.id.tv_form_result, "")
            submit.isEnabled = false
            agree.setOnCheckedChangeListener { _, checked -> submit.isEnabled = checked }
            submit.setOnClickListener {
                val shown = if (isOn(Fault.FORM_RESULT_WRONG)) name.text.toString() + "X" else name.text.toString()
                result.text = "提交成功：$shown / ${if (large.isChecked) "大杯" else "小杯"} / " +
                    if (notify.isChecked) "通知开" else "通知关"
            }
            if (isOn(Fault.FORM_SUBMIT_MISSING)) submit.visibility = View.GONE
        })
    }
}
