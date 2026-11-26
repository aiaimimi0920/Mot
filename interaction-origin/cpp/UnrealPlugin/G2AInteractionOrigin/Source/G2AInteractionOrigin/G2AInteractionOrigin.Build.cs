using System.IO;
using UnrealBuildTool;

public class G2AInteractionOrigin : ModuleRules
{
    public G2AInteractionOrigin(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        CppStandard = CppStandardVersion.Cpp17;

        PublicIncludePaths.Add(Path.Combine(ModuleDirectory, "..", "..", "..", "include"));

        PublicDependencyModuleNames.AddRange(new string[]
        {
            "Core",
            "CoreUObject",
            "Engine"
        });

        if (Target.Platform == UnrealTargetPlatform.Win64)
        {
            PublicSystemLibraries.Add("ws2_32.lib");
        }
    }
}
