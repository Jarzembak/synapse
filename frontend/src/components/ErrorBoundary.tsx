import { Component, ErrorInfo, ReactNode } from "react";

interface Props { children: ReactNode }
interface State { error: Error | null }

/** Catches render-time errors so one broken page (e.g. a malformed artifact)
 * shows a recoverable message instead of unmounting the whole SPA to a blank
 * white screen. Keyed on the route by the caller so navigating away resets it. */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("render error:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="errorboundary">
          <h2>Something went wrong rendering this page.</h2>
          <p className="error">{this.state.error.message}</p>
          <button onClick={() => this.setState({ error: null })}>try again</button>
        </div>
      );
    }
    return this.props.children;
  }
}
