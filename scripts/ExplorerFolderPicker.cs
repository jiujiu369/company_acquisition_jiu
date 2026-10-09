// Windows Common Item Dialog in folder mode.
// Contract: https://learn.microsoft.com/windows/win32/shell/common-file-dialog
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

public static class FolderPickerProgram
{
    [STAThread]
    public static int Main(string[] args)
    {
        try
        {
            // GUI applications have redirected pipes, without a console code page.
            Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new System.Text.UTF8Encoding(false)) { AutoFlush = true });
            Console.SetError(new StreamWriter(Console.OpenStandardError(), new System.Text.UTF8Encoding(false)) { AutoFlush = true });
            if (args.Length > 0 && args[0] == "--check")
            {
                uint options = ExplorerFolderPicker.CheckOptions();
                Console.Write("{\"folder_mode\":" + ((options & 0x20) != 0 ? "true" : "false") +
                    ",\"filesystem_only\":" + ((options & 0x40) != 0 ? "true" : "false") + "}");
                return 0;
            }
            Application.EnableVisualStyles();
            string path = ExplorerFolderPicker.Select(IntPtr.Zero, args.Length > 0 ? args[0] : "",
                "选择商品资料保存到的实际文件夹", "选择文件夹");
            if (path != null) Console.Write(path);
            return 0;
        }
        catch (Exception error) { Console.Error.Write(error.Message); return 1; }
    }
}

public static class ExplorerFolderPicker
{
    const uint PickFolders = 0x20, ForceFileSystem = 0x40;
    const uint NoChangeDirectory = 0x8, PathMustExist = 0x800, DontAddToRecent = 0x02000000;
    const int Canceled = unchecked((int)0x800704C7);
    [ComImport, Guid("42F85136-DB7E-439C-85F1-E4075D135FC8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IFileDialog
    {
        [PreserveSig] int Show(IntPtr owner);
        void SetFileTypes(uint count, IntPtr filters);
        void SetFileTypeIndex(uint index);
        void GetFileTypeIndex(out uint index);
        void Advise(IntPtr events, out uint cookie);
        void Unadvise(uint cookie);
        void SetOptions(uint options);
        void GetOptions(out uint options);
        void SetDefaultFolder(IShellItem folder);
        void SetFolder(IShellItem folder);
        void GetFolder(out IShellItem folder);
        void GetCurrentSelection(out IShellItem item);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetFileName(out IntPtr name);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
        void GetResult(out IShellItem item);
    }
    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellItem
    {
        void BindToHandler(IntPtr context, ref Guid handler, ref Guid iid, out IntPtr result);
        void GetParent(out IShellItem parent);
        void GetDisplayName(uint type, out IntPtr name);
        void GetAttributes(uint mask, out uint attributes);
        void Compare(IShellItem other, uint hint, out int order);
    }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHCreateItemFromParsingName(string path, IntPtr context, ref Guid iid,
        [MarshalAs(UnmanagedType.Interface)] out IShellItem item);
    static IFileDialog Create()
    {
        IFileDialog dialog = (IFileDialog)Activator.CreateInstance(Type.GetTypeFromCLSID(
            new Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")));
        try
        {
            uint options;
            dialog.GetOptions(out options);
            dialog.SetOptions(options | PickFolders | ForceFileSystem | NoChangeDirectory | PathMustExist | DontAddToRecent);
            return dialog;
        }
        catch { Marshal.FinalReleaseComObject(dialog); throw; }
    }
    // Diagnostics verify the actual shell object without displaying a dialog.
    public static uint CheckOptions()
    {
        IFileDialog dialog = Create();
        try { uint options; dialog.GetOptions(out options); return options; }
        finally { Marshal.FinalReleaseComObject(dialog); }
    }
    public static string Select(IntPtr owner, string initial, string title, string confirmLabel)
    {
        IFileDialog dialog = Create();
        IShellItem start = null, result = null;
        IntPtr name = IntPtr.Zero;
        try
        {
            dialog.SetTitle(title);
            dialog.SetOkButtonLabel(confirmLabel);
            if (!String.IsNullOrEmpty(initial) && Directory.Exists(initial))
            {
                Guid iid = typeof(IShellItem).GUID;
                SHCreateItemFromParsingName(initial, IntPtr.Zero, ref iid, out start);
                dialog.SetFolder(start);
            }
            int hr = dialog.Show(owner);
            if (hr == Canceled) return null;
            Marshal.ThrowExceptionForHR(hr);
            dialog.GetResult(out result);
            result.GetDisplayName(0x80058000, out name); // SIGDN_FILESYSPATH
            return Marshal.PtrToStringUni(name);
        }
        finally
        {
            if (name != IntPtr.Zero) Marshal.FreeCoTaskMem(name);
            if (result != null) Marshal.FinalReleaseComObject(result);
            if (start != null) Marshal.FinalReleaseComObject(start);
            Marshal.FinalReleaseComObject(dialog);
        }
    }
}
