# ViewSense Edge Worker

Worker de IA para detecção, rastreamento e contagem de pessoas/veículos em tempo real.
Utiliza YOLOv8 + ByteTrack com cruzamento de linha adaptativo.

## Funcionalidades
- Detecção de pessoas e veículos via YOLOv8
- Rastreamento por ID com ByteTrack
- Cruzamento de linha com ponto adaptativo (cabeça ↑ / centro ↓)
- Sincronização automática de configurações com o painel ViewSense
- Auto-update via cron (03:00 diariamente)

## Instalação

### Ubuntu/Debian (VPS/Servidor)
```bash
git clone https://github.com/paulosouza17/viewsense-edge-worker.git
cd viewsense-edge-worker
sudo bash install_ubuntu.sh
```

### macOS (Desenvolvimento local)
```bash
git clone https://github.com/paulosouza17/viewsense-edge-worker.git
cd viewsense-edge-worker
bash install_mac.sh
```

## Configuração
```bash
cp config.yaml.template config.yaml
# Edite config.yaml com suas credenciais do painel ViewSense
```

## Auto-Update
O worker verifica atualizações no GitHub todo dia às **03:00** automaticamente.
Para forçar uma atualização manual:
```bash
bash ~/viewsense-ai-worker/auto_update.sh
```

## Logs
```bash
pm2 logs viewsense-ai-worker
cat ~/viewsense-ai-worker/update.log
```
