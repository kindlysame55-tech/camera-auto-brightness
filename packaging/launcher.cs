using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Windows.Forms;

// Native .NET launcher: no PyInstaller extraction or console is needed here.
internal static class CamabLauncher
{
    private static string logPath;
    private static readonly object logLock = new object();

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")]
    private static extern bool ShowWindowAsync(IntPtr window, int command);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr data, int size, out int required);
    [DllImport("advapi32.dll")]
    private static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
    [DllImport("advapi32.dll")]
    private static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);

    [STAThread]
    private static int Main(string[] args)
    {
        bool check = args.Length == 1 && args[0] == "--launcher-check";
        bool quiet = check || Array.IndexOf(args, "--selftest") >= 0;
        string folder = AppDomain.CurrentDomain.BaseDirectory;
        try
        {
            string target = FindTarget(folder);
            string runtime = PrepareRuntime(folder);
            logPath = Path.Combine(runtime, "launcher.log");
            Log("Target: " + target);
            int integrity = CurrentIntegrity();
            Log("Process integrity RID: " + integrity);
#if !LAUNCHER_TEST
            if (integrity < 0x2000 && (check || !quiet))
            {
                Log("Low integrity: hardware operations are not available.");
                if (!check)
                    MessageBox.Show("程序处于 Windows 低权限环境，无法控制屏幕亮度。\n\n请从普通文件管理器启动正式版本，或将完整便携包解压到桌面文件夹后再双击。",
                        "摄像头自动亮度 · 无法调光", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 2;
            }
#endif
            if (check)
            {
                Log("Launcher check passed. TEMP/TMP: " + runtime);
                return 0;
            }

            using (Mutex gate = new Mutex(false, "Local\\CameraAutoBrightnessLauncher"))
            {
                bool acquired;
                try { acquired = gate.WaitOne(0); }
                catch (AbandonedMutexException) { acquired = true; }
                if (!acquired)
                {
                    if (!quiet && !ActivateExisting(target))
                        MessageBox.Show("程序已经在运行或正在启动。请从右下角托盘图标打开。",
                            "摄像头自动亮度", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return 0;
                }
                try
                {
                    // Also respect programs started directly, without this launcher.
                    if (!quiet && ActivateExisting(target)) return 0;
                    string workingFolder = Path.GetFullPath(folder);
                    ClearStopFlag(workingFolder);
                    ProcessStartInfo start = new ProcessStartInfo(target);
                    start.WorkingDirectory = workingFolder;
                    start.UseShellExecute = false;
                    start.CreateNoWindow = true;
                    start.RedirectStandardOutput = true;
                    start.RedirectStandardError = true;
                    start.StandardOutputEncoding = Encoding.UTF8;
                    start.StandardErrorEncoding = Encoding.UTF8;
                    start.EnvironmentVariables["TEMP"] = runtime;
                    start.EnvironmentVariables["TMP"] = runtime;
                    start.EnvironmentVariables["PYTHONUTF8"] = "1";
                    start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                    string[] forwarded = args.Length == 0 ? new string[] { "tray" } : args;
                    StringBuilder command = new StringBuilder();
                    foreach (string arg in forwarded)
                    {
                        if (command.Length > 0) command.Append(' ');
                        command.Append(Quote(arg));
                    }
                    start.Arguments = command.ToString();
                    using (Process child = new Process())
                    {
                        child.StartInfo = start;
                        child.OutputDataReceived += CaptureOutput;
                        child.ErrorDataReceived += CaptureOutput;
                        child.Start();
                        child.BeginOutputReadLine();
                        child.BeginErrorReadLine();
                        Log("Started PID " + child.Id);
                        child.WaitForExit();
                        int code = child.ExitCode;
                        Log("Exited: " + code);
                        if (code != 0 && !quiet)
                            MessageBox.Show("程序未能正常运行，退出码：" + code +
                                "\n\n启动日志：" + logPath,
                                "摄像头自动亮度", MessageBoxButtons.OK, MessageBoxIcon.Error);
                        return code;
                    }
                }
                finally { gate.ReleaseMutex(); }
            }
        }
        catch (Exception error)
        {
            Log(error.ToString());
            if (!quiet)
                MessageBox.Show("启动失败：" + error.Message +
                    "\n\n请保留完整程序文件夹，启动器需要找到 CamabTray.exe。",
                    "摄像头自动亮度", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static int CurrentIntegrity()
    {
        using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
        {
            int required;
            GetTokenInformation(identity.Token, 25, IntPtr.Zero, 0, out required);
            IntPtr data = Marshal.AllocHGlobal(required);
            try
            {
                if (!GetTokenInformation(identity.Token, 25, data, required, out required))
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                IntPtr sid = Marshal.ReadIntPtr(data);
                byte count = Marshal.ReadByte(GetSidSubAuthorityCount(sid));
                return Marshal.ReadInt32(GetSidSubAuthority(sid, (uint)(count - 1)));
            }
            finally { Marshal.FreeHGlobal(data); }
        }
    }

    private static string FindTarget(string folder)
    {
        string[] candidates = {
            Path.Combine(folder, "CamabTray.exe"),
            Path.Combine(folder, "dist", "CamabTray.exe"),
            Path.Combine(folder, "dist", "CamabTray", "CamabTray.exe")
        };
        foreach (string path in candidates)
            if (File.Exists(path)) return Path.GetFullPath(path);
        throw new FileNotFoundException("找不到 CamabTray.exe。");
    }

    private static string PrepareRuntime(string folder)
    {
        string[] candidates = {
            Path.Combine(UserDataFolder(), "runtime"),
            Path.Combine(folder, "runtime")
        };
        foreach (string candidate in candidates)
        {
            try
            {
                string path = Path.GetFullPath(candidate);
                Directory.CreateDirectory(path);
                string probe = Path.Combine(path, "write-check-" + Guid.NewGuid().ToString("N"));
                using (FileStream stream = new FileStream(probe, FileMode.CreateNew,
                    FileAccess.Write, FileShare.None, 1, FileOptions.DeleteOnClose))
                    stream.WriteByte(0);
                return path;
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        throw new IOException("没有可写的运行临时目录。请把程序文件夹放到可写位置。");
    }

    private static void ClearStopFlag(string workingFolder)
    {
        string config = Environment.GetEnvironmentVariable("CAMAB_CONFIG");
        string baseFolder;
        if (!String.IsNullOrEmpty(config))
            baseFolder = Path.GetDirectoryName(Path.IsPathRooted(config)
                ? Path.GetFullPath(config) : Path.GetFullPath(Path.Combine(workingFolder, config)));
        else baseFolder = UserDataFolder();
        string flag = Path.Combine(baseFolder, "stop.flag");
        if (File.Exists(flag)) File.Delete(flag);
    }

    private static string UserDataFolder()
    {
        string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (String.IsNullOrEmpty(local))
        {
            local = Environment.GetEnvironmentVariable("USERPROFILE");
            if (String.IsNullOrEmpty(local))
                local = Environment.GetEnvironmentVariable("HOMEDRIVE") + Environment.GetEnvironmentVariable("HOMEPATH");
            if (String.IsNullOrEmpty(local))
                local = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        }
        return Path.Combine(local, "camera-auto-brightness");
    }

    // Windows command-line quoting preserves empty strings, quotes and trailing slashes.
    private static string Quote(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"')
                result.Append('\\', slashes * 2 + 1).Append('"');
            else
                result.Append('\\', slashes).Append(c);
            slashes = 0;
        }
        result.Append('\\', slashes * 2).Append('"');
        return result.ToString();
    }

    private static bool ActivateExisting(string target)
    {
        bool found = false;
        foreach (Process process in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(target)))
        {
            using (process)
            {
                try
                {
                    if (!String.Equals(process.MainModule.FileName, target, StringComparison.OrdinalIgnoreCase))
                        continue;
                    found = true;
                    if (process.MainWindowHandle != IntPtr.Zero)
                    {
                        ShowWindowAsync(process.MainWindowHandle, 9);
                        SetForegroundWindow(process.MainWindowHandle);
                        return true;
                    }
                }
                catch (System.ComponentModel.Win32Exception) { }
                catch (InvalidOperationException) { }
            }
        }
        if (found)
            MessageBox.Show("程序已经在后台运行。请从右下角托盘图标打开。",
                "摄像头自动亮度", MessageBoxButtons.OK, MessageBoxIcon.Information);
        return found;
    }

    private static void CaptureOutput(object sender, DataReceivedEventArgs eventArgs)
    {
        if (eventArgs.Data != null)
            Log(eventArgs.Data.Length > 2000 ? eventArgs.Data.Substring(0, 2000) : eventArgs.Data);
    }

    private static void Log(string message)
    {
        if (logPath == null) return;
        lock (logLock)
        {
            try
            {
                if (File.Exists(logPath) && new FileInfo(logPath).Length > 1024 * 1024)
                    File.WriteAllText(logPath, "", Encoding.UTF8);
                File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine,
                    Encoding.UTF8);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }
}
