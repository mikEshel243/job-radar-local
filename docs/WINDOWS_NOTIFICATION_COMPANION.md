# Windows notification companion

The optional companion demonstrates a narrowly scoped, read-only Windows notification adapter. Its C# permission helper and C++/WinRT listener inspect only allowlisted application notifications and emit bounded structured records to the Python collector.

The companion requires Windows 10 or newer, Visual Studio Build Tools with MSBuild and C++ support, and Windows SDK metadata. Build it locally with:

```powershell
tools\WhatsAppNotificationListener\build.ps1 -Configuration Release
tools\WhatsAppNotificationListener\build-native-listener.ps1
```

The managed helper provides a synthetic self-test that does not access notifications:

```powershell
tools\WhatsAppNotificationListener\bin\Release\WhatsAppNotificationListener.exe --self-test
```

Packaging is optional, local, and unsigned unless the user explicitly supplies a trusted certificate thumbprint. Build output, packages, certificates, and keys are ignored and must never be committed. Installing or granting notification access is outside automated tests and CI.
