import { useState, useEffect, useMemo } from "react";
import {
  PlusOutlined,
  DeleteOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
} from "@ant-design/icons";
import { Button, Select, message } from "@agentscope-ai/design";
import { useTranslation } from "react-i18next";
import type { ModelSlotConfig } from "../../../../../api/types";
import api from "../../../../../api";
import styles from "../../index.module.less";

interface FallbackModelsSectionProps {
  providers: Array<{
    id: string;
    name: string;
    models?: Array<{ id: string; name: string }>;
    has_api_key: boolean;
    is_custom: boolean;
    is_local?: boolean;
    current_base_url?: string;
  }>;
  fallbackLlms: ModelSlotConfig[];
  onSaved: () => void;
}

export function FallbackModelsSection({
  providers,
  fallbackLlms,
  onSaved,
}: FallbackModelsSectionProps) {
  const { t } = useTranslation();

  // ---- add-row state ----
  const [addProviderId, setAddProviderId] = useState<string | undefined>();
  const [addModel, setAddModel] = useState<string | undefined>();
  const [adding, setAdding] = useState(false);

  // Reset model when provider changes
  useEffect(() => {
    setAddModel(undefined);
  }, [addProviderId]);

  // Eligible providers (configured)
  const eligible = useMemo(
    () =>
      providers.filter((p) => {
        if (p.is_local) return (p.models?.length ?? 0) > 0;
        return p.is_custom ? !!p.current_base_url : p.has_api_key;
      }),
    [providers],
  );

  const chosenProvider = providers.find((p) => p.id === addProviderId);
  const modelOptions = chosenProvider?.models ?? [];

  // ---- handlers ----

  const handleAdd = async () => {
    if (!addProviderId || !addModel) return;
    setAdding(true);
    try {
      await api.addFallback({ provider_id: addProviderId, model: addModel });
      message.success(t("models.fallbackAdded"));
      setAddProviderId(undefined);
      setAddModel(undefined);
      onSaved();
    } catch {
      message.error(t("models.fallbackAddFailed"));
    } finally {
      setAdding(false);
    }
  };

  const handleRemove = async (index: number) => {
    try {
      await api.removeFallback(index);
      message.success(t("models.fallbackRemoved"));
      onSaved();
    } catch {
      message.error(t("models.fallbackRemoveFailed"));
    }
  };

  const handleMove = async (index: number, direction: "up" | "down") => {
    const newList = [...fallbackLlms];
    const swapIdx = direction === "up" ? index - 1 : index + 1;
    if (swapIdx < 0 || swapIdx >= newList.length) return;
    [newList[index], newList[swapIdx]] = [newList[swapIdx], newList[index]];
    try {
      await api.setFallbacks(
        newList.map((s) => ({ provider_id: s.provider_id, model: s.model })),
      );
      onSaved();
    } catch {
      message.error(t("models.fallbackReorderFailed"));
    }
  };

  return (
    <div className={styles.slotSection} style={{ marginTop: 16 }}>
      {/* header */}
      <div className={styles.slotHeader}>
        <h3 className={styles.slotTitle}>{t("models.fallbackTitle")}</h3>
      </div>
      <p style={{ margin: "0 0 16px", color: "#999", fontSize: 13 }}>
        {t("models.fallbackDescription")}
      </p>

      {/* list */}
      {fallbackLlms.length === 0 ? (
        <p style={{ color: "#bbb", fontSize: 13, marginBottom: 16 }}>
          {t("models.fallbackEmpty")}
        </p>
      ) : (
        <div className={styles.modelList} style={{ marginBottom: 16 }}>
          {fallbackLlms.map((slot, idx) => (
            <div key={idx} className={styles.modelListItem}>
              <div className={styles.modelListItemInfo}>
                <span className={styles.modelListItemName}>
                  {t("models.fallbackOrder", { index: idx + 1 })}
                </span>
                <span className={styles.modelListItemId}>
                  {slot.provider_id} / {slot.model}
                </span>
              </div>
              <div className={styles.modelListItemActions}>
                <Button
                  size="small"
                  icon={<ArrowUpOutlined />}
                  disabled={idx === 0}
                  onClick={() => handleMove(idx, "up")}
                />
                <Button
                  size="small"
                  icon={<ArrowDownOutlined />}
                  disabled={idx === fallbackLlms.length - 1}
                  onClick={() => handleMove(idx, "down")}
                />
                <Button
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => handleRemove(idx)}
                />
              </div>
            </div>
          ))}
        </div>
      )}

      {/* add row */}
      <div className={styles.slotForm}>
        <div className={styles.slotField}>
          <label className={styles.slotLabel}>
            {t("models.provider")}
          </label>
          <Select
            style={{ width: "100%" }}
            placeholder={t("models.fallbackSelectProvider")}
            value={addProviderId}
            onChange={(v) => setAddProviderId(v)}
            options={eligible.map((p) => ({ value: p.id, label: p.name }))}
          />
        </div>

        <div className={styles.slotField}>
          <label className={styles.slotLabel}>{t("models.model")}</label>
          <Select
            style={{ width: "100%" }}
            placeholder={
              modelOptions.length > 0
                ? t("models.fallbackSelectModel")
                : t("models.addModelFirst")
            }
            disabled={!addProviderId || modelOptions.length === 0}
            showSearch
            optionFilterProp="label"
            value={addModel}
            onChange={(v) => setAddModel(v)}
            options={modelOptions.map((m) => ({
              value: m.id,
              label: `${m.name} (${m.id})`,
            }))}
          />
        </div>

        <div
          className={styles.slotField}
          style={{ flex: "0 0 auto", minWidth: "140px" }}
        >
          <label className={styles.slotLabel} style={{ visibility: "hidden" }}>
            {t("models.actions")}
          </label>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            loading={adding}
            disabled={!addProviderId || !addModel}
            onClick={handleAdd}
            block
          >
            {t("models.fallbackAdd")}
          </Button>
        </div>
      </div>
    </div>
  );
}
