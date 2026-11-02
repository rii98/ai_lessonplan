import { isRouteErrorResponse, Link, useRouteError } from "react-router-dom";
import { describeError } from "@/lib/api";
import { Banner, Button, Page } from "@/ui";

/** Per-route error boundary: a crash in one screen never blanks the whole app. */
export function RouteError() {
  const err = useRouteError();
  const notFound = isRouteErrorResponse(err) && err.status === 404;
  const chunk = err instanceof Error && /dynamically imported module|Loading chunk/i.test(err.message);
  return (
    <Page width="narrow">
      <Banner tone={notFound ? "info" : "danger"}
        title={notFound ? "This page doesn't exist" : chunk ? "LessonForge was updated" : "Something broke on this screen"}
        actions={<>
          {!notFound && <Button size="sm" onClick={() => window.location.reload()}>Reload</Button>}
          <Link to="/"><Button size="sm" variant="quiet">Go home</Button></Link>
        </>}>
        {notFound ? "Check the link, or go back home." : chunk ? "Reload to get the latest version." : describeError(err)}
      </Banner>
    </Page>
  );
}

export function NotFound() {
  return (
    <Page width="narrow">
      <Banner tone="info" title="This page doesn't exist" actions={<Link to="/"><Button size="sm">Go home</Button></Link>}>
        Check the link, or go back home.
      </Banner>
    </Page>
  );
}
