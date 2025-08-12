from .instance import Instance

def start_instance(instance_id):
    instance = Instance(instance_id)
    if not instance.exists():
        print(f"Instance {instance_id} not found.")
        return
    instance.start()

def stop_instance(instance_id):
    instance = Instance(instance_id)
    if not instance.exists():
        print(f"Instance {instance_id} not found.")
        return
    instance.stop()

def scli_instance(instance_id):
    instance = Instance(instance_id)
    if not instance.exists():
        print(f"Instance {instance_id} not found.")
        return
    instance.scli()
