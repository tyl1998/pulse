import type { ReactNode } from 'react';
import { Database, FileText, FolderTree, Workflow, Smartphone, ChevronRight } from 'lucide-react';
import { Badge, Button, EmptyState } from '../../shared/ui';
import { useProjectStore } from './projectStore';

function TreeSection({
  icon,
  title,
  count,
  children,
}: {
  icon: ReactNode;
  title: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <section className="project-tree-section">
      <header className="project-tree-section-header">
        {icon}
        <h3>{title}</h3>
        <span className="project-tree-count">{count}</span>
      </header>
      {children}
    </section>
  );
}

export function CaseTree() {
  const { state, store } = useProjectStore();
  const project = state.project;

  if (!project) {
    if (state.status === 'loading') {
      return (
        <p className="project-tree-status" role="status">
          正在打开本地工程…
        </p>
      );
    }
    return (
      <div className="project-tree-empty">
        {state.error ? (
          <p className="project-error" role="alert">
            {state.error}
          </p>
        ) : null}
        <EmptyState
          headingLevel={3}
          icon={<FolderTree />}
          title="尚未打开工程"
          description="打开本地测试工程或新建一个空白工程后，用例、流程、数据与环境会显示在这里。"
          action={
            <Button size="sm" variant="primary" onClick={store.openDialog}>
              打开或新建工程
            </Button>
          }
        />
      </div>
    );
  }

  const entryCases = new Set(project.entry_cases);

  return (
    <div className="project-tree" data-testid="project-case-tree">
      <header className="project-tree-header">
        <div className="project-tree-title">
          <FolderTree aria-hidden="true" />
          <strong>{project.name}</strong>
        </div>
        <Badge tone="neutral">rev {project.revision}</Badge>
        <span className="project-tree-root" title={project.root}>
          {project.root}
        </span>
        <div className="project-tree-header-actions">
          <Button size="sm" variant="ghost" onClick={store.openDialog}>
            工程管理
          </Button>
        </div>
      </header>

      <TreeSection
        icon={<FileText aria-hidden="true" />}
        title="用例"
        count={project.case_ids.length}
      >
        {project.case_ids.length === 0 ? (
          <p className="project-tree-none">没有用例</p>
        ) : (
          <ul className="project-tree-list" aria-label="用例">
            {project.case_ids.map((caseId) => {
              const summary = state.cases[caseId];
              const selected = state.selectedCaseId === caseId;
              return (
                <li key={caseId}>
                  <button
                    type="button"
                    className={`project-tree-item${selected ? ' is-selected' : ''}`}
                    aria-pressed={selected}
                    onClick={() => store.selectCase(caseId)}
                  >
                    <ChevronRight aria-hidden="true" className="project-tree-chevron" />
                    <span className="project-tree-item-text">
                      <span className="project-tree-name">{summary?.name ?? caseId}</span>
                      <span className="project-tree-id">{caseId}</span>
                    </span>
                    {entryCases.has(caseId) ? <Badge tone="info">入口</Badge> : null}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </TreeSection>

      <TreeSection
        icon={<Workflow aria-hidden="true" />}
        title="公共流程"
        count={project.flow_ids.length}
      >
        {project.flow_ids.length === 0 ? (
          <p className="project-tree-none">没有公共流程</p>
        ) : (
          <ul className="project-tree-list" aria-label="公共流程">
            {project.flow_ids.map((flowId) => (
              <li key={flowId}>
                <span className="project-tree-leaf">{flowId}</span>
              </li>
            ))}
          </ul>
        )}
      </TreeSection>

      <TreeSection
        icon={<Database aria-hidden="true" />}
        title="数据"
        count={project.data_ids.length}
      >
        {project.data_ids.length === 0 ? (
          <p className="project-tree-none">没有数据集</p>
        ) : (
          <ul className="project-tree-list" aria-label="数据集">
            {project.data_ids.map((dataId) => (
              <li key={dataId}>
                <span className="project-tree-leaf">{dataId}</span>
              </li>
            ))}
          </ul>
        )}
      </TreeSection>

      <TreeSection
        icon={<Smartphone aria-hidden="true" />}
        title="环境"
        count={project.profile_ids.length}
      >
        {project.profile_ids.length === 0 ? (
          <p className="project-tree-none">没有环境配置</p>
        ) : (
          <ul className="project-tree-list" aria-label="环境配置">
            {project.profile_ids.map((profileId) => (
              <li key={profileId}>
                <span className="project-tree-leaf">{profileId}</span>
              </li>
            ))}
          </ul>
        )}
      </TreeSection>
    </div>
  );
}

export function CaseEditorPlaceholder() {
  const { state } = useProjectStore();
  const selectedId = state.selectedCaseId;
  const selected = selectedId ? state.cases[selectedId] : undefined;

  if (!state.project) {
    return (
      <EmptyState
        headingLevel={3}
        title="尚未打开用例"
        description="从左侧用例树选择用例；打开工程后步骤编辑将在 FE-P2.8 接入。"
      />
    );
  }
  if (!selectedId) {
    return (
      <EmptyState
        headingLevel={3}
        title="请选择用例"
        description="工程已打开，请在左侧用例树选择一个入口或普通用例。"
      />
    );
  }
  return (
    <div className="project-selected-case">
      <header>
        <h3>{selected?.name ?? selectedId}</h3>
        <Badge tone={state.project.entry_cases.includes(selectedId) ? 'info' : 'neutral'}>
          {state.project.entry_cases.includes(selectedId) ? '入口用例' : '用例'}
        </Badge>
      </header>
      <p className="project-selected-id">{selectedId}</p>
      {selected?.goal ? <p>{selected.goal}</p> : null}
      <p className="project-selected-note">
        三段式、步骤与 JSON 同源编辑将在 FE-P2.8 / FE-P2.9 接入；本轮不修改用例内容。
      </p>
    </div>
  );
}
