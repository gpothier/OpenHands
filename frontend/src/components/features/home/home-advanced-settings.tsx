import { useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import { useSandboxSpecs } from "#/hooks/query/use-sandbox-specs";
import { useSelectedSandboxSpec } from "#/context/selected-sandbox-spec-context";
import { SandboxTypeSelector } from "../settings/sandbox-settings/sandbox-type-selector";
import { SettingsInput } from "../settings/settings-input";
import ChevronDownIcon from "#/icons/chevron-down-small.svg?react";
import { DEFAULT_SETTINGS } from "#/services/settings";

/**
 * Advanced settings panel for the home page.
 * Initially collapsed, contains settings like sandbox type selection.
 * Always shown so users know what sandbox type will be used.
 */
export function HomeAdvancedSettings() {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);
  const { data: specsPage } = useSandboxSpecs();
  const {
    selectedSpecId,
    setSelectedSpecId,
    selectedStorageSizeGb,
    setSelectedStorageSizeGb,
    selectedRamSizeMib,
    setSelectedRamSizeMib,
  } = useSelectedSandboxSpec();

  // Determine if the selected sandbox type is Firecracker
  const isFirecrackerSelected = useMemo(() => {
    if (!selectedSpecId || !specsPage?.items) return false;
    const selectedSpec = specsPage.items.find(
      (spec) => spec.id === selectedSpecId,
    );
    return selectedSpec?.type === "firecracker";
  }, [selectedSpecId, specsPage?.items]);

  // Don't render until we have the specs data
  if (!specsPage?.items) {
    return null;
  }

  const handleStorageSizeChange = (value: string) => {
    const sizeGb = parseInt(value, 10);
    if (!Number.isNaN(sizeGb) && sizeGb >= 8) {
      setSelectedStorageSizeGb(sizeGb);
    }
  };

  const handleRamSizeChange = (value: string) => {
    const sizeMib = parseInt(value, 10);
    if (!Number.isNaN(sizeMib) && sizeMib >= 512) {
      setSelectedRamSizeMib(sizeMib);
    }
  };

  return (
    <div className="pt-4 flex justify-center">
      <div
        className="flex flex-col gap-2 px-6 sm:max-w-full sm:min-w-full lg:px-0 lg:max-w-[703px] lg:min-w-[703px]"
        data-testid="home-advanced-settings"
      >
        <button
          type="button"
          onClick={() => setIsExpanded(!isExpanded)}
          className="flex items-center gap-2 text-sm text-[#A3A3A3] hover:text-white transition-colors"
          data-testid="advanced-settings-toggle"
        >
          <ChevronDownIcon
            width={16}
            height={16}
            className={`transition-transform duration-200 ${
              isExpanded ? "rotate-180" : ""
            }`}
          />
          <span>{t(I18nKey.COMMON$ADVANCED_SETTINGS)}</span>
        </button>

        {isExpanded && (
          <div
            className="mt-2 p-4 rounded-lg border border-[#525252] bg-[#1E1E1E] flex flex-col gap-4"
            data-testid="advanced-settings-content"
          >
            <SandboxTypeSelector
              testId="home-sandbox-type"
              label={t(I18nKey.SETTINGS$SANDBOX_TYPE)}
              selectedSpecId={selectedSpecId}
              onSelectionChange={setSelectedSpecId}
            />
            {isFirecrackerSelected && (
              <div className="flex flex-col gap-4">
                <div className="flex flex-col gap-2">
                  <SettingsInput
                    testId="home-fc-storage-size"
                    name="home-fc-storage-size"
                    type="number"
                    label={t(I18nKey.SETTINGS$STORAGE_SIZE)}
                    defaultValue={
                      selectedStorageSizeGb?.toString() ||
                      DEFAULT_SETTINGS.default_fc_storage_size_gb?.toString() ||
                      "16"
                    }
                    onChange={handleStorageSizeChange}
                    placeholder={t(I18nKey.SETTINGS$STORAGE_SIZE_GB)}
                    min={8}
                    step={1}
                    className="w-full"
                  />
                  <p className="text-xs text-[#A3A3A3]">
                    {t(I18nKey.SETTINGS$STORAGE_SIZE_DESCRIPTION)}
                  </p>
                </div>
                <div className="flex flex-col gap-2">
                  <SettingsInput
                    testId="home-fc-ram-size"
                    name="home-fc-ram-size"
                    type="number"
                    label={t(I18nKey.SETTINGS$RAM_SIZE)}
                    defaultValue={
                      selectedRamSizeMib?.toString() ||
                      DEFAULT_SETTINGS.default_fc_ram_size_mib?.toString() ||
                      "2048"
                    }
                    onChange={handleRamSizeChange}
                    placeholder={t(I18nKey.SETTINGS$RAM_SIZE_MIB)}
                    min={512}
                    step={256}
                    className="w-full"
                  />
                  <p className="text-xs text-[#A3A3A3]">
                    {t(I18nKey.SETTINGS$RAM_SIZE_DESCRIPTION)}
                  </p>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
