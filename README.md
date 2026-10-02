# Azure VM Tag Cleansing

โปรเจกต์นี้ตรวจข้อมูล tag ของ VM จาก Excel เทียบกับ Azure แล้วสร้างแผนแก้ไขก่อนสั่ง apply จริง การแก้ไข tag เกิดขึ้นเฉพาะเมื่อรันสคริปต์ 04 ด้วย `--apply` เท่านั้น

อ่านที่มา ข้อมูลในแบบสำรวจ และภาพรวมขั้นตอนใน [ภาพรวมโปรเจกต์](docs/project-overview.md)

## เตรียมเครื่อง (Windows PowerShell)

รันคำสั่งต่อไปนี้จากโฟลเดอร์โปรเจกต์ โดยใช้ Python 3.13:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

หาก PowerShell ไม่อนุญาตให้รัน `Activate.ps1` ใช้ `.\.venv\Scripts\python.exe` แทน `python` ในคำสั่งถัดไปได้ โดยไม่ต้อง activate venv

เตรียมไฟล์แบบสำรวจ Excel ไว้ในเครื่อง แล้วระบุชื่อไฟล์ใน `SURVEY_FILE` ของ `config.json` ไฟล์ Excel ไม่อยู่ใน Git หากต้องการใช้ไฟล์อื่นชั่วคราว ส่ง path ให้สคริปต์ 01 เป็น argument รูปแบบที่สคริปต์อ่านคือแถว 4 เป็น header และข้อมูลเริ่มแถว 5 โดยคอลัมน์ A–I ตามที่อธิบายใน [ภาพรวมโปรเจกต์](docs/project-overview.md)

## ตั้งค่า config

ถ้ายังไม่มี `config.json` ให้คัดลอก `config.example.json` เป็น `config.json` แล้วใส่ Azure subscription ID และตรวจค่าแบบสำรวจให้ตรงกับไฟล์ที่จะใช้:

```powershell
Copy-Item config.example.json config.json
```

ดูคีย์ทั้งหมดใน [config.example.json](config.example.json) ซึ่งใส่ชื่อไฟล์, ชื่อ sheet, จำนวน VM 102 เครื่อง และรายการ Department/Environment จากฟอร์มตัวอย่างไว้แล้ว ชื่อ sheet ต้องตรงกับ Excel ทุกตัว; หากจำนวน VM เปลี่ยน ให้แก้ `EXPECTED_VM_COUNT` หรือใช้ `null` เพื่อไม่ตรวจจำนวน ค่า `gmail.com` ใน `OWNER_DOMAIN_WHITELIST` เป็นเพียงตัวอย่าง ต้องเปลี่ยนให้ตรงกับอีเมล Owner จริง ส่วน `DEPARTMENT_SAFE_LEGACY_VALUES` คือค่าเก่าที่ระบบจะถือว่าทับได้อย่างปลอดภัย ควรตรวจรายการนี้ก่อนใช้งาน

`config.json` เป็นไฟล์ในเครื่องและถูก Git ignore รายชื่อใน `SKIP_VM` เพิ่มด้วยมือได้ การจับชื่อ VM ไม่สนตัวพิมพ์ใหญ่เล็ก สคริปต์ 01 จะเติม VM ที่คอลัมน์ `Data Confirmed?` ใน Excel เป็น `Pending` ลงใน list นี้ โดยไม่ลบชื่อเดิมและไม่เพิ่มชื่อซ้ำ หาก VM พร้อมให้แก้ tag แล้ว ให้เปลี่ยนสถานะใน Excel และลบชื่อออกจาก `SKIP_VM` ด้วยมือก่อนสร้างแผนใหม่

## ขั้นตอนการรัน

1. ตรวจข้อมูล Excel และสร้าง CSV:

   ```powershell
   python 01_vm_field_verify.py
   ```

   ตรวจ `.output/01_vm_all_data.csv` ว่ามีคอลัมน์ `Data Confirmed?` และตรวจรายการ `SKIP_VM` ใน `config.json` สคริปต์นี้สร้างไฟล์ audit และรายชื่อ owner ใน `.output` ด้วย

2. เข้าสู่ Azure เพื่อเทียบ tag ปัจจุบันกับข้อมูล Excel:

   ```powershell
   python 02_vm_tag_gap_check.py
   ```

   จะเปิด browser ให้ login แล้วสร้าง `.output/02_vm_summary.csv` พร้อมรายงานอื่นใน `.output`

3. สร้างและตรวจแผนแก้ tag:

   ```powershell
   python 03_vm_generate_tag_plan.py
   ```

   ตรวจ `.output/03_vm_tag_change_plan.json` ก่อนดำเนินการต่อ VM ใน `SKIP_VM` จะมี `tags_to_remove` ว่าง และ `tags_to_apply` มีเฉพาะ `flag=Pending Owner Review` จึงคง tag อื่นไว้ VM ที่ไม่อยู่ใน list ใช้ logic เดิม ส่วน VM ที่หาไม่พบใน Azure จะถูกแยกไป `.output/03_vm_not_found.json`

4. ทดลองอ่าน tag จริงและดู diff โดยยังไม่แก้ Azure:

   ```powershell
   python 04_apply_vm_tags.py
   ```

   เมื่อตรวจแผนและ diff แล้ว หากต้องการแก้ tag จริงจึงรัน:

   ```powershell
   python 04_apply_vm_tags.py --apply
   ```

   โหมด `--apply` ทำงานครั้งละ 10 VM หลังจบแต่ละชุด กด Enter เพื่อทำอีก 10 VM, พิมพ์ `all` แล้วกด Enter เพื่อทำชุดที่เหลือต่อเนื่องโดยไม่ถามอีก, หรือพิมพ์ `stop` แล้วกด Enter เพื่อหยุด VM ที่ยังไม่ถึงคิวจะไม่ถูกแก้ไข

   สคริปต์จะสร้าง backup `backup_vm_tags_<timestamp>.json` ก่อนเขียน tag ในแต่ละ batch และถามก่อนเริ่ม batch ถัดไป บัญชีที่ใช้ apply ต้องมีสิทธิ์แก้ tag ของ VM

5. หากต้อง rollback ให้ใช้ backup จากรอบ apply นั้น เริ่มจาก dry run:

   ```powershell
   python 05_rollback_vm_tags.py backup_vm_tags_<timestamp>.json
   python 05_rollback_vm_tags.py backup_vm_tags_<timestamp>.json --apply
   ```

   การ rollback ใช้ tag ทั้งชุดจาก backup จึงเขียนทับการแก้ tag ที่คนอื่นทำหลังรอบ apply ด้วย

## ทดสอบโดยไม่เรียก Azure

```powershell
python -m unittest discover -s tests -v
```
