# 靶场 App v1（P0.4）

被测用的 Android 应用（`com.pulse.target`），Runtime 黄金用例与已知缺陷回归共用。不是手机端服务（`pulse-android-agent`，独立仓库）。

## 验收状态

截至 2026-11-03，页面、错误开关与单测实现存在，P0.7 历史采样也曾启动 Form/List；但没有独立、可复核的 release APK 安装和设备端 intent 错误注入记录。下面的命令与观测说明是复现手册，不是已经执行的验收证据，因此 P0.4 仍为“待补证据”，不能从源码、旧构建物或 P0.7 记录推断完成。当前资源基线为一台 macOS 主机和一台 Android。

## 构建与安装

需要 JDK 17+、Android SDK（compileSdk 36）、NDK `28.2.13676358`、Flutter `3.47.9`（`flutter` 在 PATH 中）。

```bash
(cd flutter_module && flutter pub get && flutter analyze && flutter test)
./gradlew testDebugUnitTest lintRelease assembleRelease
adb -s <serial> install -r app/build/outputs/apk/release/app-release.apk
```

分发/安装请用 **release** 包（约 34 MB，Flutter AOT，使用 debug 签名）。debug 包内含 Flutter JIT 引擎，约 800 MB，部分 ROM（实测 vivo）会拒绝安装。

Flutter 页以源码方式集成（add-to-app）：`settings.gradle.kts` 引入 `flutter_module/.android/include_flutter.groovy`，因此 Gradle 构建前必须先执行 `flutter pub get`。

## 页面

| 入口 Activity          | 内容                                                       | 关键 resource-id                                                                                                                              |
| ---------------------- | ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `.MainActivity`        | 首页菜单、当前错误注入                                     | `tv_active_faults`、`btn_open_*`                                                                                                              |
| `.ListActivity`        | 60 项原生列表（行 id 相同，用于歧义/滚动）                 | `lv_items`、`tv_list_count`、`tv_list_selected`                                                                                               |
| `.FormActivity`        | 输入框、密码、单选、开关、复选；勾选协议后提交可用         | `et_name`、`et_password`、`rb_small/large`、`sw_notify`、`cb_agree`、`btn_submit`、`tv_form_result`                                           |
| `.CartActivity`        | 3 个商品、加减数量、合计、结算（默认合计 ¥107.00）         | `tv_cart_qty_N`、`btn_cart_plus/minus_N`、`tv_cart_total`、`btn_checkout`、`tv_checkout_result`                                               |
| `.WebViewActivity`     | 本地 HTML：订单号输入、查询、结果；已开启 WebContents 调试 | `wv_content`；DOM id：`order-no`、`btn-query`、`query-result`                                                                                 |
| `.DialogActivity`      | 确认弹窗、Toast                                            | `btn_show_alert`、`tv_dialog_result`、`btn_show_toast`                                                                                        |
| `.DelayedActivity`     | `delay_ms` 后出现内容                                      | `pb_loading`、`tv_delayed_content`、`btn_delayed_action`                                                                                      |
| `.FlutterPageActivity` | Flutter 积分兑换：余额 1000、3 个奖励单选、兑换、结果      | 无 resource-id；semantics 暴露为 `content-desc`（如 `可用积分：1000`）；Dart Key：`points_balance`、`reward_N`、`btn_redeem`、`redeem_result` |

## 错误注入

状态持久化在应用 SharedPreferences，跨重启保留。任一 Activity 启动时都会读取以下 extras：

| extra                    | 说明                                                        |
| ------------------------ | ----------------------------------------------------------- |
| `--ez clear_faults true` | 清空全部错误与 delay                                        |
| `--es faults "a,b"`      | 以给定集合**替换**当前错误；`""` 表示清空集合；未知名称忽略 |
| `--ei delay_ms N`        | 延迟页加载时长，默认 2000                                   |

```bash
# 推荐：启动时设置（应用未运行也可用）
adb -s <serial> shell am start -S -n com.pulse.target/.CartActivity \
  --ez clear_faults true --es faults cart_total_wrong,cart_item_missing

# 应用进程存活时可用广播（返回 result=0 成功，1 含未知名称；带当前状态）
adb -s <serial> shell am broadcast -n com.pulse.target/.FaultReceiver \
  -a com.pulse.target.SET_FAULTS --es faults popup_on_launch
```

广播限制：部分 ROM（实测 vivo Android 16）不会为已停止的应用冷启动接收器，此时广播静默无效，请用 `am start` 方式。

| 名称                     | 效果                                       |
| ------------------------ | ------------------------------------------ |
| `cart_qty_off_by_one`    | 显示数量比实际多 1（合计按实际）           |
| `cart_total_wrong`       | 显示合计多 ¥0.01                           |
| `cart_item_missing`      | 第 2 个商品不显示，合计仍包含              |
| `list_item_missing`      | 列表缺少“商品 30”                          |
| `form_submit_missing`    | 提交按钮不显示                             |
| `form_result_wrong`      | 提交结果的姓名多一个 `X`                   |
| `popup_on_launch`        | 首页启动时弹出“限时活动”                   |
| `webview_text_wrong`     | WebView 查询结果订单号多一个 `0`           |
| `delay_never_loads`      | 延迟页永不加载完成                         |
| `flutter_amount_wrong`   | Flutter 页可用积分显示多 1（实际扣减正确） |
| `flutter_button_missing` | Flutter 页兑换按钮不显示                   |

Flutter 页的错误在启动时由 `FlutterPageActivity` 作为 Dart entrypoint 参数传入，改动后需重新进入该页。

## 已知观测特性（供 Runtime 设计参考）

- 未启用无障碍服务时，`uiautomator dump` 中 WebView 只有 `wv_content` 容器，看不到 DOM 内容；这是 completeness 判定的天然样例。
- targetSdk 36 强制 edge-to-edge：应用使用无 ActionBar 主题，并按系统栏/输入法 insets 给内容加内边距。
- Flutter 页在 `uiautomator dump` 中为一组 `android.view.View`，文本在 `content-desc`，没有 resource-id，Dart `Key` 不可见；定位只能靠文本/描述或 OCR。
- 实测 `uiautomator dump` 偶发 `ERROR: null root node`（原生页和 Flutter 页都出现过），重试 1-2 次即可恢复；Runtime 不能把单次 dump 失败当成“元素不存在”。
