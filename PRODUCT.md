# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

User approved static HTML/CSS/JavaScript, deployed with GitHub Pages. The desktop product uses Python/Tkinter with native Windows, Linux and macOS adapters.

## Users

Desktop users evaluating webcam-based automatic brightness and downloading the appropriate Windows, Linux or Mac package. Windows remains the simplest portable setup; Linux and Mac require compatible device interfaces and permissions.

## Product Purpose

Explain the software in Chinese and English, show its real interface, and guide visitors to their OS download and setup instructions. Keep the Windows download, extraction and start-camab.exe flow obvious.

## Capabilities and Constraints

Camera-based light estimation with Windows WMI/DDC/CI, Linux sysfs/brightnessctl or ddcutil, and Mac brightness CLI. Gradual transitions, immediate manual control, calibration, pause and tray/menu-bar support where available. UI currently Chinese. Camera images processed locally; no normal image saving or upload. Linux permissions and compatible hardware are required. Mac brightness CLI is installed separately, third-party displays vary, and the bundle is not Apple-notarized. CI builds and synthetic tests do not establish real hardware compatibility. No license selected; do not invent one. No medical, power-saving or user-count claims. Performance claims must identify the measured analysis function and environment.

## Evidence on Hand

Source and tests under src/camab and tests. Main UI screenshot docs/images/main.png uses illustrative Windows diagnostic state. Native CI builds cover Windows x64, Linux x86_64, Mac ARM64 and Intel. Hardware-free benchmark details are in docs/performance.md. Synthetic demonstrations must be explicitly labeled; publish download links only after corresponding assets exist.

## Product Principles

Make downloading and launching obvious. Explain hardware limits honestly. Preserve working desktop software. Offer equivalent Chinese and English website content.
