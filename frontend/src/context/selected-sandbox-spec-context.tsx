import React, {
  createContext,
  useContext,
  useState,
  useEffect,
  useMemo,
} from "react";
import { useSettings } from "#/hooks/query/use-settings";
import { DEFAULT_SETTINGS } from "#/services/settings";

interface SelectedSandboxSpecContextType {
  selectedSpecId: string | null;
  setSelectedSpecId: (specId: string | null) => void;
  selectedStorageSizeGb: number | null;
  setSelectedStorageSizeGb: (sizeGb: number | null) => void;
  selectedRamSizeMib: number | null;
  setSelectedRamSizeMib: (sizeMib: number | null) => void;
  discoverAllRepos: boolean;
  setDiscoverAllRepos: (value: boolean) => void;
  skillsDiscoveryDepth: number;
  setSkillsDiscoveryDepth: (value: number) => void;
}

const SelectedSandboxSpecContext =
  createContext<SelectedSandboxSpecContextType | null>(null);

export function SelectedSandboxSpecProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const { data: settings } = useSettings();
  const [selectedSpecId, setSelectedSpecId] = useState<string | null>(null);
  const [selectedStorageSizeGb, setSelectedStorageSizeGb] = useState<
    number | null
  >(null);
  const [selectedRamSizeMib, setSelectedRamSizeMib] = useState<number | null>(
    null,
  );
  const [discoverAllRepos, setDiscoverAllRepos] = useState<boolean>(
    DEFAULT_SETTINGS.discover_all_repos ?? false,
  );
  const [skillsDiscoveryDepth, setSkillsDiscoveryDepth] = useState<number>(
    DEFAULT_SETTINGS.skills_discovery_depth ?? 1,
  );

  // Debug: log settings and state
  console.log("SelectedSandboxSpecProvider:", {
    "settings?.default_sandbox_spec_id": settings?.default_sandbox_spec_id,
    "settings?.default_fc_storage_size_gb":
      settings?.default_fc_storage_size_gb,
    "settings?.default_fc_ram_size_mib": settings?.default_fc_ram_size_mib,
    selectedSpecId,
    selectedStorageSizeGb,
    selectedRamSizeMib,
  });

  // Initialize with default from settings
  useEffect(() => {
    console.log("SelectedSandboxSpecProvider useEffect:", {
      "settings?.default_sandbox_spec_id": settings?.default_sandbox_spec_id,
      "settings?.default_fc_storage_size_gb":
        settings?.default_fc_storage_size_gb,
      "settings?.default_fc_ram_size_mib": settings?.default_fc_ram_size_mib,
      selectedSpecId,
      selectedStorageSizeGb,
      selectedRamSizeMib,
    });
    if (settings?.default_sandbox_spec_id && selectedSpecId === null) {
      console.log(
        "Setting selectedSpecId to:",
        settings.default_sandbox_spec_id,
      );
      setSelectedSpecId(settings.default_sandbox_spec_id);
    }
    // Initialize storage size with default from settings
    if (selectedStorageSizeGb === null) {
      const defaultSize =
        settings?.default_fc_storage_size_gb ??
        DEFAULT_SETTINGS.default_fc_storage_size_gb ??
        16;
      console.log("Setting selectedStorageSizeGb to:", defaultSize);
      setSelectedStorageSizeGb(defaultSize);
    }
    // Initialize RAM size with default from settings
    if (selectedRamSizeMib === null) {
      const defaultRam =
        settings?.default_fc_ram_size_mib ??
        DEFAULT_SETTINGS.default_fc_ram_size_mib ??
        2048;
      console.log("Setting selectedRamSizeMib to:", defaultRam);
      setSelectedRamSizeMib(defaultRam);
    }
  }, [
    settings?.default_sandbox_spec_id,
    settings?.default_fc_storage_size_gb,
    settings?.default_fc_ram_size_mib,
    selectedSpecId,
    selectedStorageSizeGb,
    selectedRamSizeMib,
  ]);

  // Initialize skills discovery settings from user settings
  useEffect(() => {
    if (settings?.discover_all_repos !== undefined) {
      setDiscoverAllRepos(settings.discover_all_repos);
    }
    if (settings?.skills_discovery_depth !== undefined) {
      setSkillsDiscoveryDepth(settings.skills_discovery_depth);
    }
  }, [settings?.discover_all_repos, settings?.skills_discovery_depth]);

  const contextValue = useMemo(
    () => ({
      selectedSpecId,
      setSelectedSpecId,
      selectedStorageSizeGb,
      setSelectedStorageSizeGb,
      selectedRamSizeMib,
      setSelectedRamSizeMib,
      discoverAllRepos,
      setDiscoverAllRepos,
      skillsDiscoveryDepth,
      setSkillsDiscoveryDepth,
    }),
    [
      selectedSpecId,
      selectedStorageSizeGb,
      selectedRamSizeMib,
      discoverAllRepos,
      skillsDiscoveryDepth,
    ],
  );

  return (
    <SelectedSandboxSpecContext.Provider value={contextValue}>
      {children}
    </SelectedSandboxSpecContext.Provider>
  );
}

export function useSelectedSandboxSpec() {
  const context = useContext(SelectedSandboxSpecContext);
  if (!context) {
    throw new Error(
      "useSelectedSandboxSpec must be used within a SelectedSandboxSpecProvider",
    );
  }
  return context;
}
