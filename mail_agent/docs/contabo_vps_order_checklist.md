# Чек-лист заказа Contabo Cloud VPS 10

Для заказчика: USA Central, `docker_api` + X-бот.

Страница заказа: [Cloud VPS 10](https://contabo.com/en-us/vps/cloud-vps-10/)

---

## Не менять (уже правильно)

| Параметр | Значение |
|----------|----------|
| **CPU** | 4 vCPU Cores |
| **RAM** | 8 GB RAM |
| **Port** | 200 Mbit/s Port |
| **Snapshot** | 1 Snapshot |
| **Server Quantity** | `1` |

---

## Выставить так

| Параметр | Выбор |
|----------|-------|
| **Select your term length** | `1 Month` |
| **Region** → вкладка **America** | `United States Central (St. Louis)` |
| **Storage Type** | `150 GB SSD` → **Free** |
| **Image** → вкладка **OS** → **Ubuntu** → **Version** | `Ubuntu 24.04` |
| **Data Protection with Auto Backup** | `No Data Protection` |
| **Private Networking** | `No Private Networking` |
| **Bandwidth** | `Unlimited Traffic` |
| **IPv4** | `1 IP Address` → **Free** |
| **Object Storage** | `None` |
| **Monitoring** | `None` |
| **Username** | `root` |
| **Password** | **Generate new password** → сохранить в надёжное место |

---

## Не выбирать

- **Apps**, **Panels**, **Blockchain**, **Windows Server**, **Custom Images**
- Другие **Storage Type** (300 GB SSD, NVMe и т.д.)
- **Auto Backup**, **Private Networking**, дополнительный **IPv4**
- **Object Storage**, **Monitoring**
- **Server Quantity** больше 1

---

## После оплаты — сохранить

- IP-адрес сервера
- **Password** (на email не приходит)
- Login: `root`

---

**Ориентировочная цена:** €5.50/мес + $1.80 за регион USA Central ≈ **$8/мес**.
