import React, { useEffect, useState, useRef } from "react";
import {
  Download,
  Upload,
  Shield,
  ShieldCheck,
  RefreshCw,
  FileArchive,
  Trash2,
  RotateCcw,
  AlertTriangle,
  Eye,
  EyeOff,
  CheckCircle2,
  Database,
  Inbox,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiFetch, cn } from "@/lib/utils";

interface LocalSnapshot {
  filename: string;
  label: string;
  size_bytes: number;
  created_at: string;
  total_accounts: number;
  total_microsoft_mailboxes: number;
  manifest?: Record<string, any>;
}

export default function BackupSettings() {
  // 1. Export state
  const [exportPassword, setExportPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showExportPassword, setShowExportPassword] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  const [exportSuccess, setExportSuccess] = useState("");

  // 2. Restore / Inspect state
  const [restoreFile, setRestoreFile] = useState<File | null>(null);
  const [restorePassword, setRestorePassword] = useState("");
  const [showRestorePassword, setShowRestorePassword] = useState(false);
  const [isInspecting, setIsInspecting] = useState(false);
  const [isRestoring, setIsRestoring] = useState(false);
  const [inspectManifest, setInspectManifest] = useState<Record<string, any> | null>(null);
  const [restoreError, setRestoreError] = useState("");
  const [restoreSuccess, setRestoreSuccess] = useState("");
  const [showRestoreConfirmModal, setShowRestoreConfirmModal] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 3. Local snapshots state
  const [snapshots, setSnapshots] = useState<LocalSnapshot[]>([]);
  const [isLoadingSnapshots, setIsLoadingSnapshots] = useState(false);
  const [isCreatingSnapshot, setIsCreatingSnapshot] = useState(false);
  const [snapshotActionError, setSnapshotActionError] = useState("");
  const [snapshotActionSuccess, setSnapshotActionSuccess] = useState("");

  const loadSnapshots = async () => {
    setIsLoadingSnapshots(true);
    try {
      const res = await apiFetch("/backup/snapshots");
      const data = await res.json();
      setSnapshots(data.snapshots || []);
    } catch (err: any) {
      console.error("加载快照列表失败:", err);
    } finally {
      setIsLoadingSnapshots(false);
    }
  };

  useEffect(() => {
    loadSnapshots();
  }, []);

  // Format bytes
  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  };

  // Format date
  const formatDate = (isoStr: string) => {
    try {
      const d = new Date(isoStr);
      return d.toLocaleString("zh-CN", { hour12: false });
    } catch {
      return isoStr;
    }
  };

  // 1. Handle Export
  const handleExport = async (e: React.FormEvent) => {
    e.preventDefault();
    setExportError("");
    setExportSuccess("");

    if (!exportPassword || exportPassword.length < 4) {
      setExportError("请设置至少 4 位备份密码以保护您的敏感账号数据");
      return;
    }
    if (exportPassword !== confirmPassword) {
      setExportError("两次输入的密码不一致，请核对后重试");
      return;
    }

    setIsExporting(true);
    try {
      const res = await apiFetch("/backup/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: exportPassword }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || "导出加密备份失败");
      }

      // 获取文件名
      const disposition = res.headers.get("content-disposition");
      let filename = "FreeGPT_Manager_Backup.fgmbak";
      if (disposition && disposition.includes("filename=")) {
        const match = disposition.match(/filename="?([^"]+)"?/);
        if (match && match[1]) filename = match[1];
      }

      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);

      setExportSuccess(`全量加密备份已成功生成并下载！文件名：${filename}`);
      setExportPassword("");
      setConfirmPassword("");
    } catch (err: any) {
      setExportError(err.message || "导出过程中出现异常");
    } finally {
      setIsExporting(false);
    }
  };

  // 2. Handle File Select
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setRestoreFile(e.target.files[0]);
      setInspectManifest(null);
      setRestoreError("");
      setRestoreSuccess("");
    }
  };

  // Handle Inspect (Preview)
  const handleInspect = async () => {
    setRestoreError("");
    setRestoreSuccess("");
    setInspectManifest(null);

    if (!restoreFile) {
      setRestoreError("请先选择要还原的 .fgmbak 备份文件");
      return;
    }
    if (!restorePassword) {
      setRestoreError("请输入备份文件的解密密码");
      return;
    }

    setIsInspecting(true);
    try {
      const formData = new FormData();
      formData.append("file", restoreFile);
      formData.append("password", restorePassword);

      const res = await apiFetch("/backup/inspect", {
        method: "POST",
        body: formData,
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "预检解析备份失败");
      }

      setInspectManifest(data.manifest);
    } catch (err: any) {
      setRestoreError(err.message || "解密预检出现错误");
    } finally {
      setIsInspecting(false);
    }
  };

  // Handle Real Restore
  const handleConfirmRestore = async () => {
    setShowRestoreConfirmModal(false);
    setRestoreError("");
    setRestoreSuccess("");

    if (!restoreFile || !restorePassword) {
      setRestoreError("缺少备份文件或密码");
      return;
    }

    setIsRestoring(true);
    try {
      const formData = new FormData();
      formData.append("file", restoreFile);
      formData.append("password", restorePassword);

      const res = await apiFetch("/backup/restore", {
        method: "POST",
        body: formData,
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "恢复数据库失败");
      }

      setRestoreSuccess("🎉 恭喜！数据库与凭据主密钥已成功恢复！正在刷新页面重新加载数据...");
      setTimeout(() => {
        window.location.reload();
      }, 1800);
    } catch (err: any) {
      setRestoreError(err.message || "恢复过程中出现错误");
    } finally {
      setIsRestoring(false);
    }
  };

  // 3. Create Local Snapshot
  const handleCreateSnapshot = async () => {
    setSnapshotActionError("");
    setSnapshotActionSuccess("");
    setIsCreatingSnapshot(true);
    try {
      const res = await apiFetch("/backup/snapshots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: "manual" }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "创建快照失败");

      setSnapshotActionSuccess("已成功为当前数据库创建即时本地快照！");
      await loadSnapshots();
    } catch (err: any) {
      setSnapshotActionError(err.message || "创建快照失败");
    } finally {
      setIsCreatingSnapshot(false);
    }
  };

  // Rollback Local Snapshot
  const handleRollbackSnapshot = async (filename: string) => {
    if (!window.confirm(`⚠️ 确定要将当前数据库回滚到快照 [${filename}] 吗？\n系统将在回滚前自动保留一份当前状态的安全快照。`)) {
      return;
    }

    setSnapshotActionError("");
    setSnapshotActionSuccess("");
    try {
      const res = await apiFetch(`/backup/snapshots/${encodeURIComponent(filename)}/restore`, {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "回滚快照失败");

      setSnapshotActionSuccess(`已成功回滚至快照 [${filename}]！正在重新加载数据...`);
      setTimeout(() => {
        window.location.reload();
      }, 1500);
    } catch (err: any) {
      setSnapshotActionError(err.message || "回滚过程中发生错误");
    }
  };

  // Delete Local Snapshot
  const handleDeleteSnapshot = async (filename: string) => {
    if (!window.confirm(`确定要删除本地快照 [${filename}] 吗？`)) {
      return;
    }

    setSnapshotActionError("");
    setSnapshotActionSuccess("");
    try {
      const res = await apiFetch(`/backup/snapshots/${encodeURIComponent(filename)}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "删除失败");
      }
      setSnapshots((prev) => prev.filter((s) => s.filename !== filename));
      setSnapshotActionSuccess(`已删除快照 [${filename}]`);
    } catch (err: any) {
      setSnapshotActionError(err.message || "删除快照异常");
    }
  };

  return (
    <div className="space-y-8">
      {/* CARD 1: EXPORT ENCRYPTED BACKUP */}
      <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-6 shadow-sm">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-emerald-500/10 text-emerald-400">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <div className="flex-1">
            <h2 className="text-base font-semibold text-[var(--text-primary)]">
              一键加密备份与跨机迁移包
            </h2>
            <p className="mt-1 text-xs text-[var(--text-muted)] leading-relaxed">
              将当前全部账号、任务记录、代理节点、以及<strong>微软邮箱凭据解密密钥（.microsoft_mailbox.key）</strong>进行事务级无锁打包，并通过高强度认证加密。换电脑或迁移 VPS 时在新设备上凭密码可<strong>一键 100% 完整无损还原</strong>。
            </p>
          </div>
        </div>

        <form onSubmit={handleExport} className="mt-5 space-y-4 max-w-lg">
          <div>
            <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">
              设置备份加密密码 <span className="text-red-400">*</span>
            </label>
            <div className="relative">
              <input
                type={showExportPassword ? "text" : "password"}
                value={exportPassword}
                onChange={(e) => setExportPassword(e.target.value)}
                placeholder="请输入高强度解密密码（至少4位）"
                className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-base)] px-3 py-2 pr-10 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-[var(--accent)] focus:outline-none"
              />
              <button
                type="button"
                onClick={() => setShowExportPassword(!showExportPassword)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
              >
                {showExportPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">
              确认加密密码 <span className="text-red-400">*</span>
            </label>
            <input
              type={showExportPassword ? "text" : "password"}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="请再次输入备份加密密码"
              className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-base)] px-3 py-2 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-[var(--accent)] focus:outline-none"
            />
          </div>

          {exportError && (
            <div className="flex items-center gap-2 rounded-lg bg-red-500/10 px-3 py-2 text-xs text-red-400 border border-red-500/20">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              <span>{exportError}</span>
            </div>
          )}

          {exportSuccess && (
            <div className="flex items-center gap-2 rounded-lg bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400 border border-emerald-500/20">
              <CheckCircle2 className="h-4 w-4 shrink-0" />
              <span>{exportSuccess}</span>
            </div>
          )}

          <Button type="submit" disabled={isExporting} className="gap-2">
            {isExporting ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
            {isExporting ? "正在加密打包..." : "生成并下载加密备份包 (.fgmbak)"}
          </Button>
        </form>
      </div>

      {/* CARD 2: RESTORE & PREVIEW */}
      <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-6 shadow-sm">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-500/10 text-blue-400">
            <Upload className="h-5 w-5" />
          </div>
          <div className="flex-1">
            <h2 className="text-base font-semibold text-[var(--text-primary)]">
              备份解密恢复与跨机导入
            </h2>
            <p className="mt-1 text-xs text-[var(--text-muted)] leading-relaxed">
              上传之前导出的 <code>.fgmbak</code> 备份文件并输入密码。支持<strong>免写入安全预检</strong>（先查看包内账号分布与生成时间），确认无误后再执行一键覆盖恢复。
            </p>
          </div>
        </div>

        <div className="mt-5 space-y-4 max-w-lg">
          <div>
            <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">
              选择备份文件 (.fgmbak) <span className="text-red-400">*</span>
            </label>
            <div className="flex items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept=".fgmbak"
                onChange={handleFileChange}
                className="hidden"
                id="restore-file-input"
              />
              <label
                htmlFor="restore-file-input"
                className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-base)] px-3 py-2 text-xs font-medium text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)] transition-colors"
              >
                <FileArchive className="h-4 w-4" />
                {restoreFile ? "更换备份文件" : "浏览选择备份包"}
              </label>
              <span className="text-xs text-[var(--text-muted)] truncate flex-1">
                {restoreFile ? restoreFile.name : "未选择文件"}
              </span>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">
              解密密码 <span className="text-red-400">*</span>
            </label>
            <div className="relative">
              <input
                type={showRestorePassword ? "text" : "password"}
                value={restorePassword}
                onChange={(e) => setRestorePassword(e.target.value)}
                placeholder="请输入导出时设置的备份密码"
                className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-base)] px-3 py-2 pr-10 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-[var(--accent)] focus:outline-none"
              />
              <button
                type="button"
                onClick={() => setShowRestorePassword(!showRestorePassword)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
              >
                {showRestorePassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>

          {restoreError && (
            <div className="flex items-center gap-2 rounded-lg bg-red-500/10 px-3 py-2 text-xs text-red-400 border border-red-500/20">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              <span>{restoreError}</span>
            </div>
          )}

          {restoreSuccess && (
            <div className="flex items-center gap-2 rounded-lg bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400 border border-emerald-500/20">
              <CheckCircle2 className="h-4 w-4 shrink-0" />
              <span>{restoreSuccess}</span>
            </div>
          )}

          <div className="flex flex-wrap gap-2 pt-1">
            <Button
              type="button"
              variant="outline"
              onClick={handleInspect}
              disabled={isInspecting || isRestoring || !restoreFile || !restorePassword}
              className="gap-2"
            >
              {isInspecting ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Shield className="h-4 w-4" />}
              {isInspecting ? "正在解密验证..." : "1. 安全预检 (查看包内信息)"}
            </Button>

            <Button
              type="button"
              onClick={() => setShowRestoreConfirmModal(true)}
              disabled={isRestoring || isInspecting || !restoreFile || !restorePassword}
              className="gap-2 bg-emerald-600 hover:bg-emerald-700 text-white"
            >
              {isRestoring ? <RefreshCw className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
              {isRestoring ? "正在恢复数据..." : "2. 确认恢复数据库"}
            </Button>
          </div>

          {/* INSPECTION PREVIEW CARD */}
          {inspectManifest && (
            <div className="mt-4 rounded-xl border border-[var(--border)] bg-[var(--bg-base)] p-4 space-y-3 animate-in fade-in duration-200">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-[var(--text-primary)] flex items-center gap-1.5">
                  <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                  备份验证通过 · 预检清单
                </span>
                <span className="text-[11px] font-mono text-[var(--text-muted)]">
                  版本: {inspectManifest.app_version || "未知"}
                </span>
              </div>

              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded-lg bg-[var(--bg-card)] p-2.5 border border-[var(--border)]">
                  <div className="text-[var(--text-muted)] mb-1 flex items-center gap-1">
                    <Database className="h-3.5 w-3.5 text-blue-400" />
                    账号总数
                  </div>
                  <div className="text-base font-bold text-[var(--text-primary)]">
                    {inspectManifest.total_accounts ?? 0} 个
                  </div>
                </div>

                <div className="rounded-lg bg-[var(--bg-card)] p-2.5 border border-[var(--border)]">
                  <div className="text-[var(--text-muted)] mb-1 flex items-center gap-1">
                    <Inbox className="h-3.5 w-3.5 text-amber-400" />
                    微软母体邮箱
                  </div>
                  <div className="text-base font-bold text-[var(--text-primary)]">
                    {inspectManifest.total_microsoft_mailboxes ?? 0} 个
                  </div>
                </div>
              </div>

              <div className="text-[11px] text-[var(--text-muted)] space-y-1 pt-1 border-t border-[var(--border)]">
                <div className="flex justify-between">
                  <span>导出时间:</span>
                  <span className="font-mono text-[var(--text-secondary)]">
                    {formatDate(inspectManifest.export_time)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span>包含凭据主密钥:</span>
                  <span className={cn("font-medium", inspectManifest.includes_secret_key ? "text-emerald-400" : "text-amber-400")}>
                    {inspectManifest.includes_secret_key ? "是（支持在新设备直接解密）" : "否"}
                  </span>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* CARD 3: LOCAL SNAPSHOTS */}
      <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-6 shadow-sm">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">
              本地快照与快速回退
            </h2>
            <p className="mt-1 text-xs text-[var(--text-muted)]">
              保存于本地 <code>data/snapshots/</code> 的轻量物理备份，用于在大批量操作前或恢复前提供极速安全回滚。
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            onClick={handleCreateSnapshot}
            disabled={isCreatingSnapshot}
            className="gap-2 shrink-0 self-start sm:self-auto"
          >
            {isCreatingSnapshot ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
            {isCreatingSnapshot ? "正在保存快照..." : "立即创建当前快照"}
          </Button>
        </div>

        {snapshotActionError && (
          <div className="mt-4 flex items-center gap-2 rounded-lg bg-red-500/10 px-3 py-2 text-xs text-red-400 border border-red-500/20">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>{snapshotActionError}</span>
          </div>
        )}

        {snapshotActionSuccess && (
          <div className="mt-4 flex items-center gap-2 rounded-lg bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400 border border-emerald-500/20">
            <CheckCircle2 className="h-4 w-4 shrink-0" />
            <span>{snapshotActionSuccess}</span>
          </div>
        )}

        {/* Snapshots table */}
        <div className="mt-4 overflow-hidden rounded-xl border border-[var(--border)]">
          {isLoadingSnapshots ? (
            <div className="flex items-center justify-center p-8 text-xs text-[var(--text-muted)] gap-2">
              <RefreshCw className="h-4 w-4 animate-spin" />
              正在加载本地快照...
            </div>
          ) : snapshots.length === 0 ? (
            <div className="p-8 text-center text-xs text-[var(--text-muted)]">
              当前暂无本地快照，点击上方“立即创建当前快照”即可保存。
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="bg-[var(--bg-base)] text-[var(--text-muted)] border-b border-[var(--border)]">
                  <tr>
                    <th className="px-4 py-2.5 font-medium">快照名称 / 标签</th>
                    <th className="px-4 py-2.5 font-medium">创建时间</th>
                    <th className="px-4 py-2.5 font-medium">账号总数</th>
                    <th className="px-4 py-2.5 font-medium">文件大小</th>
                    <th className="px-4 py-2.5 font-medium text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {snapshots.map((snap) => (
                    <tr key={snap.filename} className="hover:bg-[var(--bg-hover)] transition-colors">
                      <td className="px-4 py-3 font-mono text-[var(--text-primary)]">
                        <div className="font-semibold">{snap.filename}</div>
                        <div className="text-[10px] text-[var(--text-muted)]">
                          标签: <span className="text-emerald-400">{snap.label}</span>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-[var(--text-secondary)] font-mono">
                        {formatDate(snap.created_at)}
                      </td>
                      <td className="px-4 py-3 text-[var(--text-primary)]">
                        {snap.total_accounts >= 0 ? `${snap.total_accounts} 个` : "-"}
                      </td>
                      <td className="px-4 py-3 text-[var(--text-muted)] font-mono">
                        {formatSize(snap.size_bytes)}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <div className="inline-flex items-center gap-1.5">
                          <button
                            type="button"
                            onClick={() => handleRollbackSnapshot(snap.filename)}
                            className="rounded px-2 py-1 text-xs font-medium text-amber-400 hover:bg-amber-500/10 transition-colors"
                            title="回滚到此快照"
                          >
                            回退
                          </button>
                          <a
                            href={`/api/backup/snapshots/${encodeURIComponent(snap.filename)}/download`}
                            download
                            className="rounded px-2 py-1 text-xs font-medium text-blue-400 hover:bg-blue-500/10 transition-colors"
                            title="下载该 SQLite 备份文件"
                          >
                            下载
                          </a>
                          <button
                            type="button"
                            onClick={() => handleDeleteSnapshot(snap.filename)}
                            className="rounded p-1 text-[var(--text-muted)] hover:text-red-400 hover:bg-red-500/10 transition-colors"
                            title="删除快照"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* CONFIRM RESTORE MODAL */}
      {showRestoreConfirmModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-6 shadow-2xl animate-in zoom-in-95 duration-150">
            <div className="flex items-center gap-3 text-amber-400">
              <AlertTriangle className="h-6 w-6 shrink-0" />
              <h3 className="text-base font-semibold text-[var(--text-primary)]">
                确认覆盖恢复当前数据库？
              </h3>
            </div>
            <p className="mt-3 text-xs text-[var(--text-secondary)] leading-relaxed">
              恢复操作将使用备份包中的数据覆写现有数据库与凭据主密钥。为防止意外，系统在执行恢复前会<strong>全自动保留一份当前数据的安全快照</strong>。
            </p>

            {inspectManifest && (
              <div className="mt-3 rounded-lg bg-[var(--bg-base)] p-3 text-xs space-y-1 border border-[var(--border)]">
                <div>目标包账号数：<strong>{inspectManifest.total_accounts} 个</strong></div>
                <div>目标包生成时间：<strong>{formatDate(inspectManifest.export_time)}</strong></div>
              </div>
            )}

            <div className="mt-6 flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => setShowRestoreConfirmModal(false)}
              >
                取消
              </Button>
              <Button
                type="button"
                onClick={handleConfirmRestore}
                className="bg-red-600 hover:bg-red-700 text-white"
              >
                我已了解风险，确认恢复
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
