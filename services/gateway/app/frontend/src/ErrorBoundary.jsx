import {Component} from 'react';

export default class ErrorBoundary extends Component {
  state = {failed:false};
  static getDerivedStateFromError() {return {failed:true};}
  render() {
    if (this.state.failed) return <main className="login"><section role="alert"><h1>Не удалось показать страницу</h1><p>Перезагрузите приложение. Сохранённые записи останутся на месте.</p><button type="button" className="primary-action" onClick={() => location.reload()}>Перезагрузить</button></section></main>;
    return this.props.children;
  }
}
