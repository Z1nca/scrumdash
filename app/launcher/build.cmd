@echo off
rem Builds ..\..\Scrumdash.exe with the C# compiler that comes with Windows.
cd /d "%~dp0"
"%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /win32icon:logo.ico /win32manifest:Scrumdash.manifest /out:..\..\Scrumdash.exe Scrumdash.cs
