import sqlite3

con = sqlite3.connect('instance/sge_dev.db')
tablas = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('TABLAS EN LA BASE DE DATOS:')
for t in tablas:
    print(' -', t[0])

print()
print('alembic_version:', con.execute("SELECT * FROM alembic_version").fetchall())
con.close()
