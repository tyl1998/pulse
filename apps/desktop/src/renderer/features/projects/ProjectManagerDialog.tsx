import { useId, useState } from 'react';
import { Download, FolderOpen, FolderPlus, PackagePlus, RefreshCw, Server } from 'lucide-react';
import { Badge, Button, Dialog, Input } from '../../shared/ui';
import { useProjectStore } from './projectStore';

const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_-]*$/;

function suggestProjectId(name: string): string {
  const slug = name
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^[-_]+|[-_]+$/g, '');
  const base = slug || `project-${Date.now().toString(36)}`;
  return /^[A-Za-z_]/.test(base) ? base : `p-${base}`;
}

export function ProjectManagerDialog() {
  const { state, store } = useProjectStore();
  const nameId = useId();
  const idId = useId();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [projectId, setProjectId] = useState('');
  const [idTouched, setIdTouched] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const busy = state.busy !== null;
  const identifierValid = IDENTIFIER.test(projectId);

  const onCreate = async () => {
    if (!name.trim()) {
      setLocalError('工程名称不能为空');
      return;
    }
    if (!identifierValid) {
      setLocalError('工程标识必须以字母或下划线开头，且只包含字母、数字、下划线和连字符');
      return;
    }
    setLocalError(null);
    await store.createProject({ name: name.trim(), projectId });
    if (store.getSnapshot().project?.project_id === projectId) {
      setCreating(false);
    }
  };

  const current = state.project;

  return (
    <Dialog
      open={state.dialogOpen}
      onOpenChange={(open) => {
        if (!open) {
          setCreating(false);
          setName('');
          setProjectId('');
          setIdTouched(false);
          setLocalError(null);
          store.closeDialog();
        }
      }}
      title="工程管理"
      description="新建、打开、导入或导出本地测试工程。导出包会校验路径与密钥引用，不包含运行记录或同步队列。"
    >
      <div className="project-dialog" data-testid="project-manager">
        <section className="project-current" aria-label="当前工程">
          <div className="project-current-heading">
            <Server aria-hidden="true" />
            <h2>{current?.name ?? '尚未打开工程'}</h2>
            {current ? <Badge tone="info">rev {current.revision}</Badge> : null}
          </div>
          {current ? (
            <dl className="project-meta">
              <div>
                <dt>项目 ID</dt>
                <dd>{current.project_id}</dd>
              </div>
              <div>
                <dt>工程目录</dt>
                <dd title={current.root}>{current.root}</dd>
              </div>
              <div>
                <dt>内容</dt>
                <dd>
                  {current.case_ids.length} 用例 · {current.flow_ids.length} 流程 ·{' '}
                  {current.data_ids.length} 数据集 · {current.profile_ids.length} 环境
                </dd>
              </div>
            </dl>
          ) : (
            <p className="project-empty-note">
              打开已有工程，或新建一个自带启动用例的空白工程。工程发现与校验由本机 Runtime 完成。
            </p>
          )}
        </section>

        {state.error ? (
          <p className="project-error" role="alert">
            {state.error}
          </p>
        ) : null}
        {localError ? (
          <p className="project-error" role="alert">
            {localError}
          </p>
        ) : null}
        {state.notice ? (
          <p className="project-notice" role="status">
            {state.notice}
          </p>
        ) : null}

        <div className="project-actions" role="group" aria-label="工程操作">
          <Button
            disabled={busy}
            loading={state.busy === 'open'}
            onClick={() => void store.openFromDialog()}
          >
            <FolderOpen aria-hidden="true" />
            打开工程
          </Button>
          <Button
            variant="primary"
            disabled={busy}
            onClick={() => {
              setCreating(true);
              setLocalError(null);
              setName('');
              setProjectId('');
              setIdTouched(false);
            }}
          >
            <FolderPlus aria-hidden="true" />
            新建工程
          </Button>
          <Button
            disabled={busy}
            loading={state.busy === 'import'}
            onClick={() => void store.importProject()}
          >
            <PackagePlus aria-hidden="true" />
            导入工程包
          </Button>
          <Button
            disabled={busy || !current}
            loading={state.busy === 'export'}
            onClick={() => void store.exportProject()}
          >
            <Download aria-hidden="true" />
            导出工程包
          </Button>
          <Button
            variant="ghost"
            disabled={busy || !current}
            loading={state.busy === 'refresh'}
            onClick={() => void store.refresh()}
          >
            <RefreshCw aria-hidden="true" />
            刷新
          </Button>
        </div>

        {creating ? (
          <form
            className="project-create-form"
            onSubmit={(event) => {
              event.preventDefault();
              void onCreate();
            }}
          >
            <h2>新建空白工程</h2>
            <p>将创建 manifest、一个启动靶场应用的入口用例和一个 Android 本地环境。</p>
            <div className="project-field">
              <label htmlFor={nameId}>工程名称</label>
              <Input
                id={nameId}
                value={name}
                placeholder="例如：购物车回归"
                onChange={(event) => {
                  const value = event.target.value;
                  setName(value);
                  if (!idTouched) setProjectId(suggestProjectId(value));
                }}
              />
            </div>
            <div className="project-field">
              <label htmlFor={idId}>工程标识</label>
              <Input
                id={idId}
                value={projectId}
                placeholder="例如：cart-regression"
                invalid={projectId.length > 0 && !identifierValid}
                aria-describedby={`${idId}-hint`}
                onChange={(event) => {
                  setIdTouched(true);
                  setProjectId(event.target.value.trim());
                }}
              />
              <span id={`${idId}-hint`} className="project-field-hint">
                目录名与 manifest.project_id；以字母或下划线开头。
              </span>
            </div>
            <div className="project-create-actions">
              <Button
                type="submit"
                variant="primary"
                disabled={busy}
                loading={state.busy === 'create'}
              >
                选择目录并创建
              </Button>
              <Button variant="ghost" disabled={busy} onClick={() => setCreating(false)}>
                取消
              </Button>
            </div>
          </form>
        ) : null}
      </div>
    </Dialog>
  );
}
