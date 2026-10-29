# HabitFlow

HabitFlow is a local web application launched from the repository root with:

```powershell
python -m pip install -r requirements.txt
python -m habitflow
```

Use Python 3.11 on Windows. HabitFlow listens only on `127.0.0.1:5000`. If
that port is already in use, the application reports an error and does not
switch ports.

## Local data and privacy

HabitFlow is intended to keep application data on this device and does not send
it to external services. Loopback hosting does **not** protect data from other
people with access to the same device or operating-system account.
