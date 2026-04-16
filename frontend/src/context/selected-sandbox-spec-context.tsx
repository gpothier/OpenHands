import React, {
  createContext,
  useContext,
  useState,
  useEffect,
  useMemo,
} from "react";
import { useSettings } from "#/hooks/query/use-settings";

interface SelectedSandboxSpecContextType {
  selectedSpecId: string | null;
  setSelectedSpecId: (specId: string | null) => void;
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

  // Debug: log settings and state
  console.log("SelectedSandboxSpecProvider:", {
    "settings?.default_sandbox_spec_id": settings?.default_sandbox_spec_id,
    selectedSpecId,
  });

  // Initialize with default from settings
  useEffect(() => {
    console.log("SelectedSandboxSpecProvider useEffect:", {
      "settings?.default_sandbox_spec_id": settings?.default_sandbox_spec_id,
      selectedSpecId,
    });
    if (settings?.default_sandbox_spec_id && selectedSpecId === null) {
      console.log(
        "Setting selectedSpecId to:",
        settings.default_sandbox_spec_id,
      );
      setSelectedSpecId(settings.default_sandbox_spec_id);
    }
  }, [settings?.default_sandbox_spec_id, selectedSpecId]);

  const contextValue = useMemo(
    () => ({ selectedSpecId, setSelectedSpecId }),
    [selectedSpecId],
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
