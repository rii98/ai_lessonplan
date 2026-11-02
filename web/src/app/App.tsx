import { useState } from "react";
import { RouterProvider } from "react-router-dom";
import { LessonForgeProvider, type LessonForgeConfig } from "./LessonForgeProvider";
import { createRouter } from "./routes";

/** The standalone LessonForge app. */
export function App({ config, basename }: { config?: LessonForgeConfig; basename?: string }) {
  const [router] = useState(() => createRouter(basename));
  return (
    <LessonForgeProvider config={config}>
      <RouterProvider router={router} future={{ v7_startTransition: true }} />
    </LessonForgeProvider>
  );
}
