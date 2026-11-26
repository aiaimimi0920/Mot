#pragma once

#include "CoreMinimal.h"
#include "Containers/Ticker.h"
#include "Subsystems/GameInstanceSubsystem.h"
#include <memory>
#include <mutex>
#include <queue>
#include <functional>

#include "g2a/http_transport.hpp"
#include "g2a/origin_sdk.hpp"

#include "G2AOriginSubsystem.generated.h"

DECLARE_DYNAMIC_MULTICAST_DELEGATE_TwoParams(FG2ACompanionChatSignature, const FString&, Audience, const FString&, Text);

/**
 * Drop-in Unreal Engine 5 GameInstanceSubsystem hosting a G2A V0.0.1 Interaction Origin slot.
 * Marshals all Hub execute callbacks onto the Unreal Game Thread via FTSTicker.
 */
UCLASS()
class G2AINTERACTIONORIGIN_API UG2AOriginSubsystem : public UGameInstanceSubsystem
{
    GENERATED_BODY()

public:
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "G2A|Slot")
    FString SlotId = TEXT("slot-unreal-companion");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "G2A|Slot")
    FString RoleKind = TEXT("companion");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "G2A|Slot")
    FString BillingMode = TEXT("player_metered");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "G2A|Slot")
    FString FallbackHubOriginUrl = TEXT("http://127.0.0.1:19210");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "G2A|Slot")
    FString SharedOriginToken;

    UPROPERTY(BlueprintAssignable, Category = "G2A|Events")
    FG2ACompanionChatSignature OnCompanionChatDelivered;

    virtual void Initialize(FSubsystemCollectionBase& Collection) override;
    virtual void Deinitialize() override;

    UFUNCTION(BlueprintCallable, Category = "G2A|Origin")
    void SendPlayerCommand(const FString& CommandText);

    UFUNCTION(BlueprintCallable, Category = "G2A|Origin")
    void TriggerEmergencyStop();

    UFUNCTION(BlueprintCallable, Category = "G2A|Origin")
    void AdvanceEpoch();

    UFUNCTION(BlueprintCallable, Category = "G2A|Origin")
    void ReportNpcHostHandoff(const FString& NewPlayerId);

    g2a::OriginSdk* GetNativeSdk() const { return Sdk.get(); }

private:
    bool TickGameThread(float DeltaTime);

    std::unique_ptr<g2a::http::HttpHubTransportClient> Transport;
    std::unique_ptr<g2a::OriginSdk> Sdk;
    std::unique_ptr<g2a::http::LoopbackExecuteListener> Listener;
    FTSTicker::FDelegateHandle TickerHandle;
    float SyncAccumulator = 0.0f;
};
