import Navbar from "../components/Navbar";

function DashboardLayout({ children }) {
  return (
    <div className="app-layout">

      <Navbar />

      <div className="main-area">

        <main className="page-content">
          {children}
        </main>

      </div>

    </div>
  );
}

export default DashboardLayout;
