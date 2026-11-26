#include "G2AOriginSubsystem.h"

void UG2AOriginSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
    Super::Initialize(Collection);

    std::string ResolvedUrl = TCHAR_TO_UTF8(*FallbackHubOriginUrl);
    try
    {
        ResolvedUrl = g2a::OriginSdk::discover_local_hub_origin_url();
    }
    catch (...)
    {
        // Fall back cleanly to configured FallbackHubOriginUrl when hub.runtime.json is absent.
    }

    std::string TokenStd = TCHAR_TO_UTF8(*SharedOriginToken);
    Transport = std::make_unique<g2a::http::HttpHubTransportClient>(ResolvedUrl, TokenStd);
    Sdk = std::make_unique<g2a::OriginSdk>(
        TCHAR_TO_UTF8(*SlotId),
        [this](const std::string& Op, const g2a::JsonValue& Payload)
        {
            if (Transport)
            {
                Transport->post_to_hub(Op, Payload);
            }
        },
        TCHAR_TO_UTF8(*RoleKind),
        TCHAR_TO_UTF8(*BillingMode));

    Listener = std::make_unique<g2a::http::LoopbackExecuteListener>(*Sdk, 0, TokenStd);
    Sdk->publish_capabilities_and_baseline(Listener->execute_url());

    TickerHandle = FTSTicker::GetCoreTicker().AddTicker(
        FTickerDelegate::CreateUObject(this, &UG2AOriginSubsystem::TickGameThread), 0.0f);
}

void UG2AOriginSubsystem::Deinitialize()
{
    if (TickerHandle.IsValid())
    {
        FTSTicker::GetCoreTicker().RemoveTicker(TickerHandle);
    }
    if (Sdk)
    {
        try { Sdk->report_player_presence(false, 0); } catch (...) {}
    }
    Listener.reset();
    Sdk.reset();
    Transport.reset();
    Super::Deinitialize();
}

bool UG2AOriginSubsystem::TickGameThread(float DeltaTime)
{
    SyncAccumulator += DeltaTime;
    if (SyncAccumulator >= 0.2f && Sdk)
    {
        SyncAccumulator = 0.0f;
        try { Sdk->flush_dirty_sync(false); } catch (...) {}
    }
    return true;
}

void UG2AOriginSubsystem::SendPlayerCommand(const FString& CommandText)
{
    if (Sdk) Sdk->report_player_command(TCHAR_TO_UTF8(*CommandText));
}

void UG2AOriginSubsystem::TriggerEmergencyStop()
{
    if (Sdk) Sdk->report_emergency_stop();
}

void UG2AOriginSubsystem::AdvanceEpoch()
{
    if (Sdk) Sdk->advance_epoch();
}

void UG2AOriginSubsystem::ReportNpcHostHandoff(const FString& NewPlayerId)
{
    if (Sdk) Sdk->report_npc_host_handoff(TCHAR_TO_UTF8(*NewPlayerId));
}
