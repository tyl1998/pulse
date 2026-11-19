package com.pulse.target

import android.os.Bundle
import android.widget.ArrayAdapter
import android.widget.ListView
import android.widget.TextView

class ListActivity : BaseActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val items = listItems(ITEM_COUNT, skip = if (isOn(Fault.LIST_ITEM_MISSING)) 30 else null)
        val status = TextView(this)
        val list = ListView(this).apply {
            id = R.id.lv_items
            adapter = ArrayAdapter(this@ListActivity, android.R.layout.simple_list_item_1, items)
            setOnItemClickListener { _, _, pos, _ -> status.text = "已选择：${items[pos]}" }
        }
        setContentView(column(scroll = false) {
            text(R.id.tv_list_count, "共 ${items.size} 项")
            addView(status.apply { id = R.id.tv_list_selected; text = "未选择" })
            addView(list)
        })
    }

    companion object {
        const val ITEM_COUNT = 60

        fun listItems(count: Int, skip: Int?): List<String> =
            (1..count).filter { it != skip }.map { "商品 %02d".format(it) }
    }
}
