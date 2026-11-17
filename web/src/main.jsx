import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './styles.css'

// ErrorBoundary: kein weisser Screen, wenn eine Ansicht kranke Daten bekommt
class Boundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { err: null }
  }
  static getDerivedStateFromError(err) {
    return { err }
  }
  render() {
    if (this.state.err) {
      return (
        <div style={{ padding: 32, fontFamily: 'system-ui', color: '#f87171', background: '#0b1220', minHeight: '100vh' }}>
          <h2>Ups – Anzeigefehler</h2>
          <p style={{ color: '#94a3b8' }}>{String(this.state.err?.message || this.state.err)}</p>
          <button style={{ padding: '8px 16px', borderRadius: 8, border: 0, background: '#38bdf8', fontWeight: 700, cursor: 'pointer' }}
            onClick={() => this.setState({ err: null })}>Neu laden</button>
        </div>
      )
    }
    return this.props.children
  }
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <Boundary>
      <App />
    </Boundary>
  </React.StrictMode>
)
