import { Component } from "react";


/**
 * Last-resort boundary: a rendering error shows a message with a way out
 * instead of a blank page. App.jsx keys it on the route, so navigating
 * elsewhere resets it.
 */
class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("Page crashed:", error, info?.componentStack);
  }

  render() {
    if (!this.state.error) {
      return this.props.children;
    }

    return (
      <div className="page-content">
        <div className="error-state" role="alert">
          <h2>Something went wrong on this page</h2>
          <p>{this.state.error.message || String(this.state.error)}</p>
          <div className="state-actions">
            <button type="button" className="range-selector" onClick={() => window.location.reload()}>
              Reload
            </button>
            <a className="range-selector" href="/dashboard">Back to dashboard</a>
          </div>
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;
