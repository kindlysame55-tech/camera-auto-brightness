# 网站 / Website

静态软件介绍页，通过 GitHub Pages 从 `main` 分支的 `/docs` 发布。
Static software introduction, published with GitHub Pages from `/docs` on `main`.

网站 / Website: https://kindlysame55-tech.github.io/camera-auto-brightness/

本地预览 / Local preview (run from the repository root):

```powershell
python -m http.server 8765 --bind 127.0.0.1
```

打开 / Open: http://127.0.0.1:8765/docs/

`site.js` 切换中英文，并让演示滑块仅改变界面截图的明暗；不连接摄像头，不调用系统亮度接口。下载按钮指向 GitHub Release 的便携 ZIP。
`site.js` switches between Chinese and English and changes only the screenshot's brightness for the demonstration. It does not access the camera or system brightness APIs. Download links point to the portable ZIP in GitHub Releases.

字体在 `fonts/` 本地提供，附有各自的 OFL 许可文件。界面截图使用模拟诊断状态。视觉方向与三张效果图保存在 `.impeccable/`。
Fonts are self-hosted in `fonts/` with their OFL licenses. The app screenshot uses illustrative diagnostic state. The visual direction and three mockups are stored in `.impeccable/`.
