import { Autocomplete, AutocompleteItem } from "@heroui/react";
import { useTranslation } from "react-i18next";
import { useSandboxSpecs } from "#/hooks/query/use-sandbox-specs";
import type { V1SandboxSpecInfo } from "#/api/sandbox-service/sandbox-service.types";
import { cn } from "#/utils/utils";
import { I18nKey } from "#/i18n/declaration";

interface SandboxTypeSelectorProps {
  testId?: string;
  name?: string;
  label?: string;
  selectedSpecId?: string | null;
  onSelectionChange?: (specId: string | null) => void;
  wrapperClassName?: string;
  disabled?: boolean;
}

/**
 * Get the display name for a sandbox spec.
 * Falls back to the spec ID if no name is set.
 */
function getSpecDisplayName(spec: V1SandboxSpecInfo): string {
  if (spec.name) {
    return spec.name;
  }
  // Extract a readable name from the spec ID
  // e.g., "ghcr.io/openhands/agent-server:1.16.1-python::docker" -> "agent-server (docker)"
  const parts = spec.id.split("::");
  const suffix = parts.length > 1 ? ` (${parts[parts.length - 1]})` : "";
  const imagePart = parts[0];
  const imageName = imagePart.split("/").pop()?.split(":")[0] || imagePart;
  return `${imageName}${suffix}`;
}

/**
 * Get the description for a sandbox spec.
 */
function getSpecDescription(spec: V1SandboxSpecInfo): string {
  if (spec.description) {
    return spec.description;
  }
  switch (spec.type) {
    case "docker":
      return "Standard Docker container sandbox";
    case "firecracker":
      return "Firecracker microVM with KVM acceleration";
    case "remote":
      return "Remote cloud-hosted sandbox";
    case "process":
      return "Local process sandbox";
    default:
      return "";
  }
}

/**
 * Component for selecting the sandbox type (spec) for new conversations.
 */
export function SandboxTypeSelector({
  testId = "sandbox-type-selector",
  name = "sandbox-type-selector",
  label,
  selectedSpecId,
  onSelectionChange,
  wrapperClassName,
  disabled,
}: SandboxTypeSelectorProps) {
  const { t } = useTranslation();
  const { data: specsPage, isLoading, error } = useSandboxSpecs();

  const specs = specsPage?.items ?? [];

  const items = specs.map((spec) => ({
    key: spec.id,
    label: getSpecDisplayName(spec),
    description: getSpecDescription(spec),
    type: spec.type,
    kvmEnabled: spec.kvm_enabled,
  }));

  const handleSelectionChange = (key: React.Key | null) => {
    onSelectionChange?.(key?.toString() ?? null);
  };

  // If no specs are available, show a message
  if (!isLoading && specs.length === 0) {
    return (
      <div className={cn("flex flex-col gap-2.5", wrapperClassName)}>
        {label && <span className="text-sm">{label}</span>}
        <div className="text-sm text-gray-500">
          {error
            ? "Failed to load sandbox types"
            : "No sandbox types available"}
        </div>
      </div>
    );
  }

  return (
    <label className={cn("flex flex-col gap-2.5", wrapperClassName)}>
      {label && <span className="text-sm">{label}</span>}
      <Autocomplete
        aria-label={label || "Sandbox Type"}
        data-testid={testId}
        name={name}
        items={items}
        selectedKey={selectedSpecId}
        onSelectionChange={handleSelectionChange}
        isClearable
        isDisabled={disabled || isLoading}
        isLoading={isLoading}
        placeholder={isLoading ? t("HOME$LOADING") : "Select sandbox type"}
        className="w-full"
        classNames={{
          popoverContent: "bg-tertiary rounded-xl",
        }}
        inputProps={{
          classNames: {
            inputWrapper:
              "bg-tertiary border border-[#717888] h-10 w-full rounded-sm p-2 placeholder:italic",
          },
        }}
      >
        {(item) => (
          <AutocompleteItem
            key={item.key}
            textValue={item.label}
            description={item.description}
          >
            <div className="flex flex-col">
              <span className="font-medium">{item.label}</span>
              {item.description && (
                <span className="text-xs text-gray-500">
                  {item.description}
                </span>
              )}
              {item.kvmEnabled && (
                <span className="text-xs text-blue-500 mt-0.5">
                  {t(I18nKey.SETTINGS$SANDBOX_KVM_ENABLED)}
                </span>
              )}
            </div>
          </AutocompleteItem>
        )}
      </Autocomplete>
    </label>
  );
}
