import type { RouteObject } from "react-router";

import { ProtectedRoute, PublicOnlyRoute } from "../auth/RouteGuards";
import { AppLayout } from "../components/layout/AppLayout";
import { DashboardPage } from "../pages/DashboardPage";
import { LoginPage } from "../pages/LoginPage";
import { NewTranscriptionPage } from "../pages/NewTranscriptionPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { RecordPage } from "../pages/RecordPage";
import { RegisterPage } from "../pages/RegisterPage";
import { TranscriptionDetailPage } from "../pages/TranscriptionDetailPage";

export const routes: RouteObject[] = [
  {
    element: <PublicOnlyRoute />,
    children: [
      { path: "/login", element: <LoginPage /> },
      { path: "/register", element: <RegisterPage /> },
    ],
  },
  {
    element: <ProtectedRoute />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { index: true, element: <DashboardPage /> },
          { path: "/record", element: <RecordPage /> },
          { path: "/transcriptions/new", element: <NewTranscriptionPage /> },
          { path: "/transcriptions/:id", element: <TranscriptionDetailPage /> },
          { path: "*", element: <NotFoundPage /> },
        ],
      },
    ],
  },
];
