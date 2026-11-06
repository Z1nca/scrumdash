// Starts Scrumdash without any console window, and shows the Windows folder window for it.
// Build it with app/launcher/build.cmd.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("Scrumdash")]
[assembly: AssemblyProduct("Scrumdash")]
[assembly: AssemblyDescription("Scrumdash")]
[assembly: AssemblyVersion("0.1.0.0")]
[assembly: AssemblyFileVersion("0.1.0.0")]

[ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
class FileOpenDialog { }

[ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IShellItem
{
    void BindToHandler(IntPtr pbc, IntPtr bhid, IntPtr riid, out IntPtr ppv);
    void GetParent(out IShellItem ppsi);
    void GetDisplayName(uint sigdn, out IntPtr name);
    void GetAttributes(uint mask, out uint attributes);
    void Compare(IShellItem psi, uint hint, out int order);
}

// Only the first part of this Windows list is used; its order must stay as it is.
[ComImport, Guid("d57c7288-d4ad-4768-be02-9d969532d960"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IFileOpenDialog
{
    [PreserveSig] int Show(IntPtr parent);
    void SetFileTypes(uint count, IntPtr types);
    void SetFileTypeIndex(uint index);
    void GetFileTypeIndex(out uint index);
    void Advise(IntPtr events, out uint cookie);
    void Unadvise(uint cookie);
    void SetOptions(uint options);
    void GetOptions(out uint options);
    void SetDefaultFolder(IShellItem item);
    void SetFolder(IShellItem item);
    void GetFolder(out IShellItem item);
    void GetCurrentSelection(out IShellItem item);
    void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
    void GetFileName(out IntPtr name);
    void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
    void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
    void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
    void GetResult(out IShellItem item);
}

static class Program
{
    const uint PickFolders = 0x20, FileSystemOnly = 0x40, FileSystemPath = 0x80058000;

    static bool Started(string program, string arguments, string folder)
    {
        try
        {
            ProcessStartInfo info = new ProcessStartInfo(program, arguments);
            info.WorkingDirectory = folder;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            using (Process p = Process.Start(info))
            {
                // Still running after a moment means it works. A stand-in Python ends at once with an error.
                return !p.WaitForExit(1500) || p.ExitCode == 0;
            }
        }
        catch (Exception)
        {
            return false;
        }
    }

    // Prints the chosen folder for app.py, as UTF-8 so every folder name survives. Prints nothing when cancelled.
    static void PickFolder()
    {
        using (Form owner = new Form())
        {
            owner.TopMost = true;
            owner.ShowInTaskbar = false;
            owner.Opacity = 0;
            owner.StartPosition = FormStartPosition.CenterScreen;
            owner.Show();
            owner.Activate();
            IFileOpenDialog dialog = (IFileOpenDialog)new FileOpenDialog();
            uint options;
            dialog.GetOptions(out options);
            dialog.SetOptions(options | PickFolders | FileSystemOnly);
            dialog.SetTitle("Choose your project folder");
            if (dialog.Show(owner.Handle) != 0)
                return;
            IShellItem item;
            IntPtr name;
            dialog.GetResult(out item);
            item.GetDisplayName(FileSystemPath, out name);
            byte[] bytes = Encoding.UTF8.GetBytes(Marshal.PtrToStringUni(name));
            Marshal.FreeCoTaskMem(name);
            using (Stream output = Console.OpenStandardOutput())
                output.Write(bytes, 0, bytes.Length);
        }
    }

    [STAThread]
    static void Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--pick-folder")
        {
            PickFolder();
            return;
        }
        string folder = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "app");
        if (!File.Exists(Path.Combine(folder, "app.py")))
        {
            MessageBox.Show("Scrumdash.exe needs the \"app\" folder next to it. Keep them together, or download Scrumdash again.", "Scrumdash");
            return;
        }
        string own = Path.Combine(folder, "python", "pythonw.exe");
        if (File.Exists(own) && Started(own, "app.py", folder))
            return;
        if (Started("pyw", "-3 app.py", folder) || Started("pythonw", "app.py", folder) || Started("python", "app.py", folder))
            return;
        if (MessageBox.Show("Scrumdash could not find Python. Download Scrumdash again, or install Python 3.10 or newer.\n\nOpen the Python download page?", "Scrumdash", MessageBoxButtons.YesNo) == DialogResult.Yes)
            Process.Start("https://www.python.org/downloads/");
    }
}
