
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import LoginPage from "./pages/LoginPage";
import { isAuthenticated } from "./lib/auth";

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  return isAuthenticated() ? children : <Navigate to="/login" replace />;
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route
          path="/login"
          element={
            isAuthenticated() ? <Navigate to="/documents" replace /> : <LoginPage />
          }
        />

        <Route
          path="/documents"
          element={
            <ProtectedRoute>
              <div className="min-h-screen bg-slate-950 p-8 text-white">
                Documents dashboard coming next.
              </div>
            </ProtectedRoute>
          }
        />

        <Route path="*" element={<Navigate to="/documents" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
