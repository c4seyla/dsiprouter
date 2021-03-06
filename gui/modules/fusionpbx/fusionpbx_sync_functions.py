import psycopg2
import MySQLdb
import os
import subprocess
import docker 

#Obtain a set of FusionPBX systems that contains domains that Kamailio will route traffic to.
def get_sources(db):

    #Dictionary object to hold the set of source FusionPBX systems 
    sources = {}
    
    #Kamailio Database Parameters
    kam_hostname=db['hostname']
    kam_username=db['username']
    kam_password=db['password']
    kam_database=db['database']

    try:
        db=MySQLdb.connect(host=kam_hostname, user=kam_username, passwd=kam_password, db=kam_database)
        c=db.cursor()
        c.execute("""select pbx_id,address as pbx_ip,db_ip,db_username,db_password from dsip_fusionpbx_db join dr_gateways on dsip_fusionpbx_db.pbx_id=dr_gateways.gwid where enabled=1""")
        results =c.fetchall()
        db.close()
        for row in results:
            #Store the PBX_ID as the key and the entire row as the value
            sources[row[1]] =row
    except Exception as e:
        print(e)
   
    return sources

#Will remove all of the domain data so that it can be rebuilt
def delete_domain_tables(db):

    #Kamailio Database Parameters
    kam_hostname=db['hostname']
    kam_username=db['username']
    kam_password=db['password']
    kam_database=db['database']
    
    try:
        db=MySQLdb.connect(host=kam_hostname, user=kam_username, passwd=kam_password, db=kam_database)
        c=db.cursor()
        c.execute("truncate domain")
        c.execute("truncate domain_attrs")
        db.commit()
    except Exception as e:
        print(e)

def sync_db(source,dest):
   
    #FusionPBX Database Parameters 
    pbx_id =source[0]
    pbx_ip =source[1]
    fpbx_hostname=source[2]
    fpbx_username=source[3]
    fpbx_password=source[4]
    fpbx_database = 'fusionpbx'

    #Kamailio Database Parameters
    kam_hostname=dest['hostname']
    kam_username=dest['username']
    kam_password=dest['password']
    kam_database=dest['database']
     
   
    #Get a connection to Kamailio Server DB
    db=MySQLdb.connect(host=kam_hostname, user=kam_username, passwd=kam_password, db=kam_database)

    #Trying connecting to PostgresSQL database using a Trust releationship first
    try:
        conn = psycopg2.connect(dbname=fpbx_database, user=fpbx_username, host=fpbx_hostname, password=fpbx_password)
        cur = conn.cursor()
        cur.execute("""select domain_name from v_domains where domain_enabled='true'""")
        rows = cur.fetchall()
        if rows is not None:
            c=db.cursor()
            for row in rows:
                c.execute("""insert ignore into domain (id,domain,did) values (null,%s,%s)""", (row[0],row[0]))
                #c.execute("""delete from domain_attrs where did=%s""",(row[0]))
                c.execute("""insert ignore into domain_attrs (id,did,name,type,value) values (null,%s,'pbx_ip',2,%s)""", (row[0],pbx_ip))
            c.execute("""update dsip_fusionpbx_db set syncstatus=1, lastsync=NOW() where pbx_id=%s""",(pbx_id,))
            db.commit()                
    except Exception as e:
        c=db.cursor()
        c.execute("""update dsip_fusionpbx_db set syncstatus=0, lastsync=NOW(), syncerror=%s where pbx_id=%s""",(str(e),pbx_id))
        db.commit()
        print(e)

def reloadkam(kamcmd_path):
       try:
          #subprocess.call(['kamcmd' ,'permissions.addressReload'])
          #subprocess.call(['kamcmd','drouting.reload'])
          subprocess.call([kamcmd_path,'domain.reload'])
          return True
       except:
           return False

def update_nginx(sources):

    print("Updating Nginx")
    #Connect to docker
    client = docker.from_env()
    
    # If there isn't any FusionPBX sources then just shutdown the container
    if len(sources) < 1:
        containers = client.containers.list()
        for container in containers:
            if container.name == "dsiprouter-nginx":
                #Stop the container
                container.stop()
                container.remove(force=True)
                print("Stopped nginx container")
        return
       
    #Create the Nginx file

    serverList = ""
    for source in sources:
        serverList += "server " + str(source) + ";\n"

    #print(serverList)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    print(script_dir)

    input = open(script_dir + "/dsiprouter.nginx.tpl")
    output = open(script_dir + "/dsiprouter.nginx","w")
    output.write(input.read().replace("##SERVERLIST##", serverList))
    output.close()
    input.close()


    #Check if dsiprouter-nginx is running. If so, reload nginx

    containers = client.containers.list()
    for container in containers:
        if container.name == "dsiprouter-nginx":
            #Execute a command to reload nginx
            container.exec_run("nginx -s reload")
            print("Reloaded nginx") 
            return

    #Start the container if one is not running
    try:
           print("trying to create a container") 
           host_volume_path = script_dir + "/dsiprouter.nginx"
           html_volume_path = script_dir + "/html"
           #host_volume_path = script_dir
           print(host_volume_path)
           client.containers.run(image='nginx:latest',
		name="dsiprouter-nginx",
		ports={'80/tcp':'80/tcp'},
                volumes={host_volume_path: {'bind':'/etc/nginx/conf.d/default.conf','mode':'rw'},html_volume_path: {'bind':'/etc/nginx/html','mode':'rw'}},
		detach=True)
           print("created a container") 
    except Exception as e: 
    
           print(e)
                


def run_sync(settings):
    
    #Set the system where sync'd data will be stored.  
    #The Kamailio DB in our case


    #If already running - don't run

    if os.path.isfile("./.sync-lock"):
        print("Already running")
        return

    else:
        f=open("./.sync-lock","w+")
        f.close()

    
    dest={}
    dest['hostname']=settings.KAM_DB_HOST
    dest['username']=settings.KAM_DB_USER
    dest['password']=settings.KAM_DB_PASS
    dest['database']=settings.KAM_DB_NAME

    #Get the list of FusionPBX's that needs to be sync'd
    sources = get_sources(dest)
    print(sources)
    #Remove all existing domain and domain_attrs entries 
    delete_domain_tables(dest)
 
    #Loop thru each FusionPBX system and start the sync
    for key in sources:
        sync_db(sources[key],dest)
     
    #Reload Kamailio
    reloadkam(settings.KAM_KAMCMD_PATH)

    #Update Nginx configuration file for HTTP Provisioning and start docker container if we have FusionPBX systems
    #update_nginx(sources[key])
    if sources is not None:
        sources = list(sources.keys())
        update_nginx(sources)

    #Remove lock file
    os.remove("./.sync-lock") 

def main():
   

    #Set the system where sync'd data will be stored.  
    #The Kamailio DB in our case

    dest={}
    dest['hostname']='localhost'
    dest['username']='kamailio'
    dest['password']='kamailiorw'
    dest['database']='kamailio'

    #Get the list of FusionPBX's that needs to be sync'd
    sources = get_sources(dest)

    #Remove all existing domain and domain_attrs entries 
    delete_domain_tables(dest)
 
    #Loop thru each FusionPBX system and start the sync
    for key in sources:
        sync_db(sources[key],dest)

    #Reload Kamailio
    reloadkam('/usr/sbin/kamcmd')


if __name__== "__main__":
    run_sync()
