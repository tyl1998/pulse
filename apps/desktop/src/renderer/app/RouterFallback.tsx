import { Link } from '@tanstack/react-router';
import { Button, EmptyState } from '../shared/ui';

export function NotFoundPage() {
  return (
    <div className="route-page">
      <EmptyState
        headingLevel={1}
        title="页面不存在"
        description="这个地址没有对应的应用壳路由。可以返回工作台继续。"
        action={
          <Link to="/" className="ui-button" data-variant="primary">
            返回工作台
          </Link>
        }
      />
    </div>
  );
}

export function RouteErrorPage({ error }: { error: unknown }) {
  return (
    <div className="route-page">
      <EmptyState
        headingLevel={1}
        title="页面加载失败"
        description={error instanceof Error ? error.message : '发生未知错误。'}
        action={
          <Button variant="primary" onClick={() => window.location.reload()}>
            重新加载
          </Button>
        }
      />
    </div>
  );
}
