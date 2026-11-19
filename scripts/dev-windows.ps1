$ErrorActionPreference = 'Stop'

# A Windows job survives intermediate parent exits. Closing our only job handle kills
# the complete owned tree. Start suspended so no descendant can escape assignment.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class PulseDevJob {
    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimits {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinWorkingSet, MaxWorkingSet;
        public uint ActiveProcesses;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct IoCounters { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)]
    struct ExtendedLimits {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct StartupInfo {
        public uint Size;
        public string Reserved, Desktop, Title;
        public uint X, Y, XSize, YSize, XChars, YChars, Fill, Flags;
        public ushort Show, ReservedSize;
        public IntPtr ReservedData, Input, Output, Error;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo { public IntPtr Process, Thread; public uint Pid, Tid; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits limits, uint size);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcess(string app, StringBuilder command, IntPtr pa, IntPtr ta,
        bool inherit, uint flags, IntPtr env, string cwd, ref StartupInfo startup, out ProcessInfo info);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")]
    static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll")]
    static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")]
    static extern IntPtr GetStdHandle(int kind);

    public static int Run(string command, string marker) {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new Win32Exception();
        ProcessInfo child = new ProcessInfo();
        try {
            ExtendedLimits limits = new ExtendedLimits();
            limits.Basic.Flags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(limits)))
                throw new Win32Exception();
            StartupInfo startup = new StartupInfo();
            startup.Size = (uint)Marshal.SizeOf(startup);
            startup.Flags = 0x100; // STARTF_USESTDHANDLES
            startup.Input = GetStdHandle(-10);
            startup.Output = GetStdHandle(-11);
            startup.Error = GetStdHandle(-12);
            if (!CreateProcess(null, new StringBuilder(command), IntPtr.Zero, IntPtr.Zero,
                true, 4, IntPtr.Zero, Environment.CurrentDirectory, ref startup, out child))
                throw new Win32Exception();
            if (!AssignProcessToJobObject(job, child.Process)) throw new Win32Exception();
            Console.Out.WriteLine(marker + "{\"type\":\"owned-pid\",\"pid\":" + child.Pid + "}");
            if (ResumeThread(child.Thread) == UInt32.MaxValue) throw new Win32Exception();
            if (WaitForSingleObject(child.Process, UInt32.MaxValue) != 0) throw new Win32Exception();
            uint code;
            if (!GetExitCodeProcess(child.Process, out code)) throw new Win32Exception();
            return unchecked((int)code);
        } finally {
            if (child.Process != IntPtr.Zero) {
                // Also covers failures before the suspended process was assigned to the job.
                TerminateProcess(child.Process, 1);
                CloseHandle(child.Process);
            }
            if (child.Thread != IntPtr.Zero) CloseHandle(child.Thread);
            CloseHandle(job);
        }
    }
}
'@

try {
    $command = ConvertFrom-Json $env:PULSE_DEV_COMMAND
    Remove-Item Env:\PULSE_DEV_COMMAND
    [Environment]::CurrentDirectory = (Get-Location).Path
    $code = [PulseDevJob]::Run($command, $env:PULSE_DEV_MARKER)
    exit $code
} catch {
    [Console]::Error.WriteLine('Pulse owned-process supervisor failed: ' + $_.Exception.Message)
    exit 1
}
