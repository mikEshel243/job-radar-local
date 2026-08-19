#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <ctime>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <windows.h>

#include <winrt/Windows.ApplicationModel.h>
#include <winrt/Windows.Data.Json.h>
#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Security.Cryptography.Core.h>
#include <winrt/Windows.Security.Cryptography.h>
#include <winrt/Windows.UI.Notifications.h>
#include <winrt/Windows.UI.Notifications.Management.h>
#include <winrt/base.h>

using namespace winrt;
using namespace Windows::ApplicationModel;
using namespace Windows::Data::Json;
using namespace Windows::Foundation;
using namespace Windows::Security::Cryptography;
using namespace Windows::Security::Cryptography::Core;
using namespace Windows::UI::Notifications;
using namespace Windows::UI::Notifications::Management;

namespace
{
    constexpr int ProtocolVersion = 1;
    constexpr size_t MaxConfigBytes = 16384;
    constexpr uint32_t MaxAppIdChars = 512;
    constexpr uint32_t MaxGroupNameChars = 256;
    constexpr uint32_t MaxGroupIdChars = 100;
    constexpr uint32_t MaxBodyLines = 16;
    constexpr uint32_t MaxBodyLineChars = 4096;
    constexpr uint32_t MaxBodyChars = 32768;
    constexpr size_t MaxRememberedIds = 4096;
    // Public package-family verification hash, not a credential.
    constexpr wchar_t OfficialPackageFamilyHash[] =
        L"3ddc73ce38b0b441b3991404beda228de64ad9fb08495a67c9a3b027ebad3116"; // pragma: allowlist secret

    struct Config
    {
        hstring groupName;
        hstring groupIdentifier;
        int pollIntervalSeconds = 5;
        int maxNotificationsPerPoll = 200;
    };

    struct Options
    {
        bool listen = false;
        bool once = false;
        bool diagnostic = false;
        bool checkAccess = false;
        bool probeAppInfo = false;
        std::filesystem::path configPath;
    };

    struct PollCounts
    {
        int totalNotifications = 0;
        int applicationIdentityErrors = 0;
        int applicationInfoErrors = 0;
        int officialPackageFamilyMatches = 0;
        int appUserModelIdErrors = 0;
        int reconstructedApplicationIdentities = 0;
        int allowedAppNotifications = 0;
        int exactGroupNotifications = 0;
        int acceptedNotifications = 0;
        int oversizedNotifications = 0;
        int visualInspectionErrors = 0;
        std::map<uint32_t, int> applicationInfoErrorCodes;
    };

    std::string Lowercase(std::string value)
    {
        std::transform(
            value.begin(),
            value.end(),
            value.begin(),
            [](unsigned char character)
            {
                return static_cast<char>(std::tolower(character));
            });
        return value;
    }

    std::string Sha256Hex(std::string const& value)
    {
        auto provider =
            HashAlgorithmProvider::OpenAlgorithm(L"SHA256");
        auto input = CryptographicBuffer::ConvertStringToBinary(
            to_hstring(value),
            BinaryStringEncoding::Utf8);
        auto digest = provider.HashData(input);
        return Lowercase(to_string(
            CryptographicBuffer::EncodeToHexString(digest)));
    }

    bool IsOfficialPackageFamily(AppInfo const& appInfo)
    {
        std::string observed =
            Sha256Hex(to_string(appInfo.PackageFamilyName()));
        return observed == to_string(OfficialPackageFamilyHash);
    }

    int RequireInteger(
        JsonObject const& root,
        wchar_t const* name,
        int minimum,
        int maximum)
    {
        double value = root.GetNamedNumber(name);
        if (!std::isfinite(value) ||
            value < minimum ||
            value > maximum ||
            std::floor(value) != value)
        {
            throw hresult_invalid_argument();
        }

        return static_cast<int>(value);
    }

    hstring RequireText(
        JsonObject const& root,
        wchar_t const* name,
        uint32_t maximum)
    {
        hstring value = root.GetNamedString(name);

        if (value.empty() || value.size() > maximum)
        {
            throw hresult_invalid_argument();
        }

        return value;
    }

    Config LoadConfig(std::filesystem::path const& path)
    {
        std::ifstream input(path, std::ios::binary | std::ios::ate);

        if (!input)
        {
            throw hresult_error(
                HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND));
        }

        std::streamsize size = input.tellg();

        if (size <= 0 ||
            static_cast<size_t>(size) > MaxConfigBytes)
        {
            throw hresult_invalid_argument();
        }

        input.seekg(0);
        std::string content(static_cast<size_t>(size), '\0');

        if (!input.read(content.data(), size))
        {
            throw hresult_error(E_FAIL);
        }

        JsonObject root = JsonObject::Parse(to_hstring(content));

        if (root.Size() != 6 ||
            RequireInteger(
                root,
                L"version",
                ProtocolVersion,
                ProtocolVersion) != ProtocolVersion)
        {
            throw hresult_invalid_argument();
        }

        Config config;
        (void)RequireText(
            root,
            L"app_user_model_id",
            MaxAppIdChars);
        config.groupName = RequireText(
            root,
            L"group_name",
            MaxGroupNameChars);
        config.groupIdentifier = RequireText(
            root,
            L"group_identifier",
            MaxGroupIdChars);
        config.pollIntervalSeconds = RequireInteger(
            root,
            L"poll_interval_seconds",
            2,
            300);
        config.maxNotificationsPerPoll = RequireInteger(
            root,
            L"max_notifications_per_poll",
            1,
            500);
        return config;
    }

    Options ParseOptions(int argc, wchar_t* argv[])
    {
        Options options;

        for (int index = 1; index < argc; index++)
        {
            std::wstring argument = argv[index];

            if (argument == L"--listen")
            {
                options.listen = true;
            }
            else if (argument == L"--once")
            {
                options.once = true;
            }
            else if (argument == L"--diagnostic")
            {
                options.diagnostic = true;
            }
            else if (argument == L"--check-access")
            {
                options.checkAccess = true;
            }
            else if (argument == L"--probe-app-info")
            {
                options.probeAppInfo = true;
            }
            else if (argument == L"--config")
            {
                index++;

                if (index >= argc)
                {
                    throw hresult_invalid_argument();
                }

                options.configPath = argv[index];
            }
            else
            {
                throw hresult_invalid_argument();
            }
        }

        if (options.checkAccess || options.probeAppInfo)
        {
            if (options.listen ||
                options.once ||
                options.diagnostic ||
                !options.configPath.empty() ||
                (
                    options.checkAccess
                    && options.probeAppInfo
                ))
            {
                throw hresult_invalid_argument();
            }

            return options;
        }

        if (!options.listen || options.configPath.empty())
        {
            throw hresult_invalid_argument();
        }

        if (options.diagnostic && !options.once)
        {
            throw hresult_invalid_argument();
        }

        return options;
    }

    std::string FormatUtc(DateTime const& value)
    {
        auto systemTime = clock::to_sys(value);
        auto wholeSeconds =
            std::chrono::time_point_cast<std::chrono::seconds>(
                systemTime);
        auto fraction =
            std::chrono::duration_cast<
                std::chrono::duration<int64_t, std::ratio<1, 10000000>>>(
                systemTime - wholeSeconds)
                .count();
        std::time_t time =
            std::chrono::system_clock::to_time_t(wholeSeconds);
        std::tm utc{};
        gmtime_s(&utc, &time);
        std::ostringstream output;
        output << std::put_time(&utc, "%Y-%m-%dT%H:%M:%S")
               << "."
               << std::setw(7)
               << std::setfill('0')
               << fraction
               << "Z";
        return output.str();
    }

    std::string BuildSourceMessageId(
        hstring const& appUserModelId,
        hstring const& groupIdentifier,
        uint32_t notificationId,
        std::string const& creationTime)
    {
        std::ostringstream fingerprint;
        fingerprint << to_string(appUserModelId)
                    << "\n"
                    << to_string(groupIdentifier)
                    << "\n"
                    << notificationId
                    << "\n"
                    << creationTime;
        return "wa_notification_" +
            Sha256Hex(fingerprint.str());
    }

    hstring ReconstructAppUserModelId(AppInfo const& appInfo)
    {
        hstring family = appInfo.PackageFamilyName();
        hstring id = appInfo.Id();

        if (family.empty() ||
            id.empty() ||
            family.size() + id.size() + 1 > MaxAppIdChars)
        {
            return {};
        }

        return family + L"!" + id;
    }

    void WriteNotificationRecord(
        Config const& config,
        std::string const& sourceMessageId,
        std::string const& creationTime,
        std::vector<hstring> const& bodyLines)
    {
        JsonArray body;

        for (hstring const& line : bodyLines)
        {
            body.Append(JsonValue::CreateStringValue(line));
        }

        JsonObject record;
        record.Insert(
            L"type",
            JsonValue::CreateStringValue(L"notification"));
        record.Insert(
            L"protocol_version",
            JsonValue::CreateNumberValue(ProtocolVersion));
        record.Insert(
            L"group_identifier",
            JsonValue::CreateStringValue(config.groupIdentifier));
        record.Insert(
            L"source_message_id",
            JsonValue::CreateStringValue(
                to_hstring(sourceMessageId)));
        record.Insert(
            L"message_date",
            JsonValue::CreateStringValue(
                to_hstring(creationTime)));
        record.Insert(L"body_lines", body);
        std::cout << to_string(record.Stringify()) << std::endl;
    }

    std::wstring ErrorCategory(uint32_t code)
    {
        std::wostringstream output;
        output << L"HResultError, HRESULT 0x"
               << std::uppercase
               << std::hex
               << std::setw(8)
               << std::setfill(L'0')
               << code;
        return output.str();
    }

    void WriteDiagnostic(PollCounts const& counts)
    {
        JsonObject categories;

        for (auto const& [code, count] :
             counts.applicationInfoErrorCodes)
        {
            categories.Insert(
                ErrorCategory(code),
                JsonValue::CreateNumberValue(count));
        }

        JsonObject record;
        record.Insert(
            L"type",
            JsonValue::CreateStringValue(L"diagnostic"));
        record.Insert(
            L"protocol_version",
            JsonValue::CreateNumberValue(ProtocolVersion));
        record.Insert(
            L"total_notifications",
            JsonValue::CreateNumberValue(
                counts.totalNotifications));
        record.Insert(
            L"application_identity_errors",
            JsonValue::CreateNumberValue(
                counts.applicationIdentityErrors));
        record.Insert(
            L"application_info_errors",
            JsonValue::CreateNumberValue(
                counts.applicationInfoErrors));
        record.Insert(
            L"application_info_error_categories",
            categories);
        record.Insert(
            L"official_package_family_matches",
            JsonValue::CreateNumberValue(
                counts.officialPackageFamilyMatches));
        record.Insert(
            L"app_user_model_id_errors",
            JsonValue::CreateNumberValue(
                counts.appUserModelIdErrors));
        record.Insert(
            L"reconstructed_application_identities",
            JsonValue::CreateNumberValue(
                counts.reconstructedApplicationIdentities));
        record.Insert(
            L"allowed_app_notifications",
            JsonValue::CreateNumberValue(
                counts.allowedAppNotifications));
        record.Insert(
            L"exact_group_notifications",
            JsonValue::CreateNumberValue(
                counts.exactGroupNotifications));
        record.Insert(
            L"accepted_notifications",
            JsonValue::CreateNumberValue(
                counts.acceptedNotifications));
        record.Insert(
            L"oversized_notifications",
            JsonValue::CreateNumberValue(
                counts.oversizedNotifications));
        record.Insert(
            L"visual_inspection_errors",
            JsonValue::CreateNumberValue(
                counts.visualInspectionErrors));
        std::cout << to_string(record.Stringify()) << std::endl;
    }

    PollCounts PollOnce(
        UserNotificationListener const& listener,
        Config const& config,
        std::set<std::string>& emittedIds,
        std::deque<std::string>& emittedOrder,
        bool emitAccepted,
        bool rememberAccepted)
    {
        auto notifications =
            listener.GetNotificationsAsync(NotificationKinds::Toast)
                .get();
        PollCounts counts;
        counts.totalNotifications =
            static_cast<int>(notifications.Size());
        uint32_t boundedCount = (std::min)(
            notifications.Size(),
            static_cast<uint32_t>(
                config.maxNotificationsPerPoll));

        for (uint32_t index = 0;
             index < boundedCount;
             index++)
        {
            UserNotification notification = notifications.GetAt(index);
            AppInfo appInfo{nullptr};

            try
            {
                appInfo = notification.AppInfo();
            }
            catch (hresult_error const& error)
            {
                counts.applicationInfoErrors++;
                counts.applicationIdentityErrors++;
                counts.applicationInfoErrorCodes[
                    static_cast<uint32_t>(
                        error.code().value)]++;
                continue;
            }

            if (!appInfo)
            {
                counts.applicationIdentityErrors++;
                continue;
            }

            try
            {
                if (!IsOfficialPackageFamily(appInfo))
                {
                    continue;
                }

                counts.officialPackageFamilyMatches++;
            }
            catch (...)
            {
                counts.applicationIdentityErrors++;
                continue;
            }

            hstring appId;

            try
            {
                appId = appInfo.AppUserModelId();
            }
            catch (...)
            {
                counts.appUserModelIdErrors++;
                appId = {};
            }

            if (appId.empty())
            {
                try
                {
                    appId = ReconstructAppUserModelId(appInfo);
                }
                catch (...)
                {
                    appId = {};
                }

                if (appId.empty())
                {
                    counts.applicationIdentityErrors++;
                    continue;
                }

                counts.reconstructedApplicationIdentities++;
            }

            try
            {
                counts.allowedAppNotifications++;
                auto binding =
                    notification.Notification()
                        .Visual()
                        .GetBinding(
                            KnownNotificationBindings::ToastGeneric());

                if (!binding)
                {
                    continue;
                }

                auto textElements = binding.GetTextElements();

                if (textElements.Size() == 0 ||
                    textElements.GetAt(0).Text() != config.groupName)
                {
                    continue;
                }

                counts.exactGroupNotifications++;
                std::vector<hstring> bodyLines;
                uint32_t bodyChars = 0;
                bool oversized = false;

                for (uint32_t textIndex = 1;
                     textIndex < textElements.Size();
                     textIndex++)
                {
                    hstring line =
                        textElements.GetAt(textIndex).Text();

                    if (bodyLines.size() >= MaxBodyLines ||
                        line.size() > MaxBodyLineChars ||
                        bodyChars + line.size() > MaxBodyChars)
                    {
                        oversized = true;
                        break;
                    }

                    bodyChars += static_cast<uint32_t>(line.size());
                    bodyLines.push_back(line);
                }

                bool hasText = std::any_of(
                    bodyLines.begin(),
                    bodyLines.end(),
                    [](hstring const& line)
                    {
                        return !line.empty();
                    });

                if (oversized || bodyLines.empty() || !hasText)
                {
                    counts.oversizedNotifications++;
                    continue;
                }

                counts.acceptedNotifications++;
                std::string creationTime =
                    FormatUtc(notification.CreationTime());
                std::string sourceMessageId =
                    BuildSourceMessageId(
                        appId,
                        config.groupIdentifier,
                        notification.Id(),
                        creationTime);

                if (rememberAccepted)
                {
                    auto inserted =
                        emittedIds.insert(sourceMessageId);

                    if (!inserted.second)
                    {
                        continue;
                    }

                    emittedOrder.push_back(sourceMessageId);

                    while (emittedOrder.size() > MaxRememberedIds)
                    {
                        emittedIds.erase(emittedOrder.front());
                        emittedOrder.pop_front();
                    }
                }

                if (emitAccepted)
                {
                    WriteNotificationRecord(
                        config,
                        sourceMessageId,
                        creationTime,
                        bodyLines);
                }
            }
            catch (...)
            {
                counts.visualInspectionErrors++;
                continue;
            }
        }

        return counts;
    }

    int RunProbe(UserNotificationListener const& listener)
    {
        auto notifications =
            listener.GetNotificationsAsync(NotificationKinds::Toast)
                .get();
        int successes = 0;
        int nulls = 0;
        int errors = 0;
        std::map<uint32_t, int> errorCounts;

        for (UserNotification const& notification : notifications)
        {
            try
            {
                auto appInfo = notification.AppInfo();
                appInfo ? successes++ : nulls++;
            }
            catch (hresult_error const& error)
            {
                errors++;
                errorCounts[
                    static_cast<uint32_t>(
                        error.code().value)]++;
            }
        }

        std::cout << "Native AppInfo probe complete.\n";
        std::cout << "Toast notifications observed: "
                  << notifications.Size() << "\n";
        std::cout << "AppInfo successes: " << successes << "\n";
        std::cout << "AppInfo null results: " << nulls << "\n";
        std::cout << "AppInfo errors: " << errors << "\n";

        for (auto const& [code, count] : errorCounts)
        {
            std::cout << "AppInfo HRESULT: 0x"
                      << std::uppercase
                      << std::hex
                      << std::setw(8)
                      << std::setfill('0')
                      << code
                      << std::dec
                      << "; count: "
                      << count
                      << "\n";
        }

        return 0;
    }
}

int wmain(int argc, wchar_t* argv[])
{
    init_apartment(apartment_type::multi_threaded);

    try
    {
        UserNotificationListener listener =
            UserNotificationListener::Current();

        Options options = ParseOptions(argc, argv);

        if (options.checkAccess)
        {
            auto status = listener.GetAccessStatus();
            std::cout << "Notification listener access: "
                      << static_cast<int>(status)
                      << "\n";
            return status ==
                    UserNotificationListenerAccessStatus::Allowed
                ? 0
                : 2;
        }

        if (listener.GetAccessStatus() !=
            UserNotificationListenerAccessStatus::Allowed)
        {
            std::cerr
                << "Native notification access is not allowed.\n";
            return 2;
        }

        if (options.probeAppInfo)
        {
            return RunProbe(listener);
        }

        Config config = LoadConfig(options.configPath);
        std::set<std::string> emittedIds;
        std::deque<std::string> emittedOrder;

        if (options.diagnostic)
        {
            PollCounts counts = PollOnce(
                listener,
                config,
                emittedIds,
                emittedOrder,
                false,
                false);
            WriteDiagnostic(counts);
            return 0;
        }

        if (options.once)
        {
            (void)PollOnce(
                listener,
                config,
                emittedIds,
                emittedOrder,
                true,
                true);
            return 0;
        }

        (void)PollOnce(
            listener,
            config,
            emittedIds,
            emittedOrder,
            false,
            true);

        while (true)
        {
            std::this_thread::sleep_for(
                std::chrono::seconds(
                    config.pollIntervalSeconds));
            (void)PollOnce(
                listener,
                config,
                emittedIds,
                emittedOrder,
                true,
                true);
        }
    }
    catch (hresult_error const& error)
    {
        std::cerr << "Native listener failed with HRESULT 0x"
                  << std::uppercase
                  << std::hex
                  << std::setw(8)
                  << std::setfill('0')
                  << static_cast<uint32_t>(
                         error.code().value)
                  << "\n";
        return 1;
    }
    catch (...)
    {
        std::cerr
            << "Native listener failed with an unexpected error.\n";
        return 1;
    }
}
