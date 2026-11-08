"use client";

import { useEffect, useRef, useState } from "react";
import { label } from "../lib/api";

export function Status({ value }: { value: string }) {
  const tone =
    value === "ready_for_review" ||
    value === "approved" ||
    value === "passed" ||
    value === "eligible"
      ? "success"
      : value === "failed" ||
          value === "rejected" ||
          value === "exhausted" ||
          value === "ineligible"
        ? "danger"
        : value === "queued" || value === "running" || value === "cancelling"
          ? "active"
          : "neutral";
  return (
    <span className={`status status-${tone}`}>
      <span className="status-dot" />
      {label(value)}
    </span>
  );
}

export function Notice({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="notice" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="text-button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function ReviewDialog({
  action,
  busy,
  error,
  onClose,
  onSubmit,
}: {
  action: "approve" | "reject" | null;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (reason: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState("");
  useEffect(() => {
    if (action) {
      setReason("");
      dialog.current?.showModal();
    } else {
      dialog.current?.close();
    }
  }, [action]);
  return (
    <dialog
      ref={dialog}
      className="review-dialog"
      onClose={onClose}
      onCancel={(event) => {
        if (busy) event.preventDefault();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (reason.trim()) onSubmit(reason.trim());
        }}
      >
        <p className="eyebrow">REVIEW DECISION</p>
        <h2>{action === "approve" ? "Approve patch" : "Reject patch"}</h2>
        <p className="muted">
          This records your decision. It does not change the source project.
        </p>
        <label htmlFor="review-reason">
          Reason <span className="required">required</span>
        </label>
        <textarea
          id="review-reason"
          required
          minLength={1}
          maxLength={2000}
          value={reason}
          onChange={(event) => {
            setReason(event.target.value);
          }}
          rows={4}
          disabled={busy}
          autoFocus
        />
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button
            type="button"
            className="button button-quiet"
            onClick={onClose}
            disabled={busy}
          >
            Keep reviewing
          </button>
          <button
            className={`button ${action === "reject" ? "button-danger" : "button-primary"}`}
            disabled={busy || !reason.trim()}
          >
            {busy
              ? "Saving…"
              : action === "approve"
                ? "Approve patch"
                : "Reject patch"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
