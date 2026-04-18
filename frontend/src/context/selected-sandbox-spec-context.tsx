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

  // Debug: log settings and state
  console.log("SelectedSandboxSpecProvider:", {
    "settings?.default_sandbox_spec_id": settings?.default_sandbox_spec_id,
    "settings?.default_fc_storage_size_gb":
      settings?.default_fc_storage_size_gb,
    selectedSpecId,
    selectedStorageSizeGb,
  });

  // Initialize with default from settings
  useEffect(() => {
    console.log("SelectedSandboxSpecProvider useEffect:", {
      "settings?.default_sandbox_spec_id": settings?.default_sandbox_spec_id,
      "settings?.default_fc_storage_size_gb":
        settings?.default_fc_storage_size_gb,
      selectedSpecId,
      selectedStorageSizeGb,
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
  }, [
    settings?.default_sandbox_spec_id,
    settings?.default_fc_storage_size_gb,
    selectedSpecId,
    selectedStorageSizeGb,
  ]);

  const contextValue = useMemo(
    () => ({
      selectedSpecId,
      setSelectedSpecId,
      selectedStorageSizeGb,
      setSelectedStorageSizeGb,
    }),
    [selectedSpecId, selectedStorageSizeGb],
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
