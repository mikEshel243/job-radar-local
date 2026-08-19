using System;
using System.Globalization;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Text;
using Windows.Data.Xml.Dom;
using Windows.UI.Notifications;
using Windows.UI.Notifications.Management;

namespace JobRadar.WhatsAppNotificationListener
{
    internal static class Program
    {
        private sealed class Options
        {
            internal bool RequestAccess { get; set; }
            internal bool CheckAccess { get; set; }
            internal bool SelfTest { get; set; }
            internal bool EmitSyntheticToast { get; set; }
        }

        private sealed class SafeCompanionException : Exception
        {
            internal SafeCompanionException(string message)
                : base(message)
            {
            }
        }

        private static int Main(string[] args)
        {
            Console.InputEncoding = new UTF8Encoding(false);
            Console.OutputEncoding = new UTF8Encoding(false);

            try
            {
                Options options = ParseOptions(args);

                if (options.SelfTest)
                {
                    RunSelfTests();
                    Console.WriteLine(
                        "Synthetic companion self-tests passed.");
                    return 0;
                }

                if (options.EmitSyntheticToast)
                {
                    EmitSyntheticToast();
                    Console.WriteLine(
                        "Synthetic local notification emitted.");
                    return 0;
                }

                UserNotificationListener listener =
                    UserNotificationListener.Current;

                if (options.RequestAccess)
                {
                    UserNotificationListenerAccessStatus status =
                        listener.RequestAccessAsync()
                            .AsTask()
                            .GetAwaiter()
                            .GetResult();
                    return PrintAccessStatus(status);
                }

                return PrintAccessStatus(
                    listener.GetAccessStatus());
            }
            catch (Exception error)
            {
                Console.Error.WriteLine(
                    "Notification companion failed: " +
                    RedactedErrorMessage(error));
                return 1;
            }
        }

        private static int PrintAccessStatus(
            UserNotificationListenerAccessStatus status)
        {
            Console.WriteLine(
                "Windows notification access status: " +
                status.ToString());
            return status ==
                UserNotificationListenerAccessStatus.Allowed
                ? 0
                : 2;
        }

        private static string RedactedErrorMessage(Exception error)
        {
            if (error is UnauthorizedAccessException)
            {
                return "access was denied.";
            }

            if (error is SafeCompanionException)
            {
                return error.Message;
            }

            if (error is ArgumentException)
            {
                return "command-line arguments were invalid.";
            }

            return "an unexpected local Windows API error occurred (" +
                SafeExceptionIdentity(error) + ").";
        }

        private static string SafeExceptionIdentity(Exception error)
        {
            return error.GetType().Name +
                ", HRESULT 0x" +
                unchecked((uint)error.HResult).ToString(
                    "X8",
                    CultureInfo.InvariantCulture);
        }

        private static ToastNotification BuildSyntheticToast()
        {
            XmlDocument content =
                ToastNotificationManager.GetTemplateContent(
                    ToastTemplateType.ToastText02);
            XmlNodeList textNodes =
                content.GetElementsByTagName("text");

            if (textNodes.Length < 2)
            {
                throw new SafeCompanionException(
                    "Synthetic notification template is invalid.");
            }

            textNodes[0].AppendChild(
                content.CreateTextNode(
                    "Job Radar synthetic identity test"));
            textNodes[1].AppendChild(
                content.CreateTextNode(
                    "Fixed local content; no private data."));

            return new ToastNotification(content)
            {
                Tag = "job-radar-identity-test",
                ExpirationTime =
                    DateTimeOffset.Now.AddMinutes(10)
            };
        }

        private static void EmitSyntheticToast()
        {
            ToastNotificationManager.CreateToastNotifier()
                .Show(BuildSyntheticToast());
        }

        private static Options ParseOptions(string[] args)
        {
            Options options = new Options();

            foreach (string argument in args)
            {
                switch (argument)
                {
                    case "--request-access":
                        options.RequestAccess = true;
                        break;
                    case "--check-access":
                        options.CheckAccess = true;
                        break;
                    case "--self-test":
                        options.SelfTest = true;
                        break;
                    case "--emit-synthetic-toast":
                        options.EmitSyntheticToast = true;
                        break;
                    default:
                        throw new ArgumentException(
                            "Unknown command-line argument.");
                }
            }

            int primaryModes =
                (options.RequestAccess ? 1 : 0) +
                (options.CheckAccess ? 1 : 0) +
                (options.SelfTest ? 1 : 0) +
                (options.EmitSyntheticToast ? 1 : 0);

            if (primaryModes != 1)
            {
                throw new ArgumentException(
                    "Choose exactly one companion mode.");
            }

            return options;
        }

        private static void RunSelfTests()
        {
            Options access = ParseOptions(
                new[] { "--check-access" });
            Options toast = ParseOptions(
                new[] { "--emit-synthetic-toast" });

            if (!access.CheckAccess ||
                access.RequestAccess ||
                !toast.EmitSyntheticToast)
            {
                throw new SafeCompanionException(
                    "Command-line mode self-test failed.");
            }

            bool rejectedEmpty = false;
            bool rejectedCombined = false;

            try
            {
                ParseOptions(Array.Empty<string>());
            }
            catch (ArgumentException)
            {
                rejectedEmpty = true;
            }

            try
            {
                ParseOptions(
                    new[]
                    {
                        "--check-access",
                        "--request-access"
                    });
            }
            catch (ArgumentException)
            {
                rejectedCombined = true;
            }

            if (!rejectedEmpty || !rejectedCombined)
            {
                throw new SafeCompanionException(
                    "Command-line rejection self-test failed.");
            }

            const string privateFixture =
                "SYNTHETIC_PRIVATE_EXCEPTION_TEXT";
            string redacted = RedactedErrorMessage(
                new Exception(privateFixture));

            if (redacted.Contains(privateFixture))
            {
                throw new SafeCompanionException(
                    "Error redaction self-test failed.");
            }

            ToastNotification notification =
                BuildSyntheticToast();

            if (!String.Equals(
                notification.Tag,
                "job-radar-identity-test",
                StringComparison.Ordinal))
            {
                throw new SafeCompanionException(
                    "Synthetic notification self-test failed.");
            }
        }
    }
}
