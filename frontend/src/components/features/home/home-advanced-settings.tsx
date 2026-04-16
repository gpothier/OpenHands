import { useState } from "react";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import { useSandboxSpecs } from "#/hooks/query/use-sandbox-specs";
import { useSelectedSandboxSpec } from "#/context/selected-sandbox-spec-context";
import { SandboxTypeSelector } from "../settings/sandbox-settings/sandbox-type-selector";
import ChevronDownIcon from "#/icons/chevron-down-small.svg?react";

/**
 * Advanced settings panel for the home page.
 * Initially collapsed, contains settings like sandbox type selection.
 * Only shown when there are settings to display (e.g., multiple sandbox types).
 */
export function HomeAdvancedSettings() {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);
  const { data: specsPage } = useSandboxSpecs();
  const { selectedSpecId, setSelectedSpecId } = useSelectedSandboxSpec();

  // Only show if there are advanced settings to display
  const hasMultipleSandboxTypes = (specsPage?.items?.length ?? 0) > 1;

  if (!hasMultipleSandboxTypes) {
    return null;
  }

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
            className="mt-2 p-4 rounded-lg border border-[#525252] bg-[#1E1E1E]"
            data-testid="advanced-settings-content"
          >
            <SandboxTypeSelector
              testId="home-sandbox-type"
              label={t(I18nKey.SETTINGS$SANDBOX_TYPE)}
              selectedSpecId={selectedSpecId}
              onSelectionChange={setSelectedSpecId}
            />
          </div>
        )}
      </div>
    </div>
  );
}
