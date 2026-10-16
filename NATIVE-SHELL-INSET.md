# 原生壳沉浸式接入说明

旅行记忆网页已经处理好安全区，但**纯 CSS 的 `env(safe-area-inset-*)` 在原生 WebView 壳里恒为 0**。

原因：iOS Safari 和 Android Chrome 会通过 `viewport-fit=cover` 把系统栏高度传给网页，
所以网页的 CSS `env()` 能生效。但原生壳（Android `Activity` + `WebView`，鸿蒙
`Ability` + `ArkWeb`）是**自己的窗口**，系统栏要么盖在 WebView 上方，要么由壳自己
padding —— 网页侧的 `env()` 读到的永远是 0。

于是就是你截图里的现象：状态栏的时间/电量压在网页顶栏的「体验账号 / 行程名」上。

## 解决方案

网页侧已就绪（`static/safearea.js`）。**壳只需要做一件事：把真实的 inset 告诉网页。**

```
window.__tmSetInsets(top, bottom)   // 单位：CSS px（已经除过 density/dpr）
```

调用后网页会：

- 顶栏 padding 加上 `top`，并把状态栏那一片涂成顶栏自己的底色（不会漏出下层）
- 底栏、悬浮按钮、`+` 号按钮一起让开 `bottom`（手势条）
- 居中弹窗整体下移 `top`

---

## Android（Kotlin）

### 1. 依赖

```kotlin
implementation("androidx.core:core-ktx:1.12.0")
```

### 2. Activity 里把 inset 喂给网页

```kotlin
import android.os.Bundle
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat

class MainActivity : ComponentActivity() {
    private lateinit var webView: WebView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 允许内容画到系统栏下方（沉浸式的前提）
        WindowCompat.setDecorFitsSystemWindows(window, false)

        webView = WebView(this)
        setContentView(webView)
        webView.settings.javaScriptEnabled = true
        webView.webViewClient = WebViewClient()
        webView.loadUrl("https://top001.xin/app")

        val density = resources.displayMetrics.density

        ViewCompat.setOnApplyWindowInsetsListener(webView) { _, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or
                WindowInsetsCompat.Type.displayCutout()
            )
            // px → CSS px
            val topCss = bars.top / density
            val bottomCss = bars.bottom / density
            webView.evaluateJavascript(
                "window.__tmSetInsets($topCss, $bottomCss);",
                null
            )
            insets   // 必须返回原 insets，不要 consume
        }
    }
}
```

> ⚠️ **不要 `consume()`**。WebView 自己也要用这份 insets 做内部处理，
> consume 掉会导致滚动区域也不对。

### 3. 沉浸式（可选，Android 10+）

如果希望状态栏透明、图标反白：

```kotlin
if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
    window.setDecorFitsSystemWindows(false)
    window.insetsController?.let {
        it.hide(android.view.WindowInsets.Type.statusBars())  // 全屏时
        it.systemBarsBehavior =
            android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
    }
}
```

如果**不想全屏**（保留状态栏，只是要避让），删掉 `hide()` 那行即可 ——
网页侧的 padding 依然生效。

---

## 鸿蒙 ArkWeb（ArkTS）

```typescript
import { webview } from '@kit.ArkWeb';
import { window } from '@kit.ArkUI';

@Entry
@Component
struct Index {
  controller: webview.WebviewController = new webview.WebviewController();
  private win?: window.Window;

  aboutToAppear(): void {
    // 允许内容延伸到系统栏区域
    window.getLastWindow(getContext(this)).then((win) => {
      this.win = win;
      win.setWindowLayoutFullScreen(true);
      this.pushInsets();
    }).catch((e) => { console.error('getLastWindow failed', e); });
  }

  private pushInsets(): void {
    if (!this.win) return;
    try {
      const area = this.win.getWindowAvoidArea(window.AvoidAreaType.TYPE_SYSTEM);
      // 单位是 vp（鸿蒙的逻辑像素），与网页 CSS px 一致，不需要再换算
      const top = area.topRect.height;
      const bottom = area.bottomRect.height;
      this.controller.runJavaScript(`window.__tmSetInsets(${top}, ${bottom});`);
    } catch (e) {
      console.error('getWindowAvoidArea failed', e);
    }
  }

  build() {
    Web({ src: 'https://top001.xin/app', controller: this.controller })
      .javaScriptAccess(true)
      .width('100%')
      .height('100%')
  }
}
```

鸿蒙的 `AvoidArea` 返回的**已经是 vp（逻辑像素）**，和 CSS px 是 1:1，
所以不用像 Android 那样除 density。

如果用的是 `Web` 组件而非 `WebviewController`，可以改用：

```typescript
Web({ src: '...', javaScriptAccess: true })
  .onPageEnd(() => { this.pushInsets(); })
```

---

## 排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 完全没变化 | JS 没执行 | 确认 `javaScriptEnabled`（Android）/ `javaScriptAccess(true)`（鸿蒙） |
| 值偏大/偏小 | 单位没换 | Android 记得 `/ density`；鸿蒙**不要**换算 |
| 双重留白 | 壳自己也加了 padding | 二选一：要么壳避让，要么网页避让 |
| 横屏时顶部留白 | 状态栏在侧边 | 横屏建议壳自己处理左右，或调 `__tmSetInsets(0, bottom)` |
| 键盘弹出时底部跳动 | 键盘也算 inset | 壳里过滤掉 `ime()` 类型，只传 `systemBars()` |

## 兜底

壳暂时没接这个接口时，网页**也会自己猜**：`display-mode: standalone` 或
`fullscreen` 下按经验值顶开（顶 28~32px、底 12~16px）。

所以即使壳里一行代码都不改，装成独立 App 打开时也不会压字 —— 只是不如
真实 inset 精确。刘海屏机型（宽 ≥360）会用偏保守的值，宁可稍多留白。

## 相关文件

| 文件 | 作用 |
|---|---|
| `static/safearea.js` | 接收 `__tmSetInsets`，写 `--tm-inset-*` CSS 变量 |
| `static/style.css` 末尾 | `.tm-native-inset` 规则，消费这些变量 |
| `<meta name="tm-safe-area" content="1">` | 已标注「网页自己处理安全区」，壳**不要**再补 |